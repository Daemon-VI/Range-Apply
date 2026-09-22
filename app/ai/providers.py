"""Providers behind the gateway. Adding one never touches a caller.

* ``stub``   – never calls anything; the default. Answers ``None`` so the
               gateway reports UNAVAILABLE (or DISABLED before reaching it).
* ``scripted`` – tests only: canned answers / failures.
* ``gemini`` – Google AI Studio free tier (the provider Phases 2/4 used).
* ``ollama`` – a local model server on the candidate's machine (no data leaves).

A provider receives a prompt and returns text. It knows nothing about
operations, caches, budgets or tenants; the gateway owns those.
"""

import json
import logging
from typing import Any, Callable, Optional, Protocol

import httpx

from app.ai.models import AIUsage, ProviderResult

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, timeout: bool = False, retryable: bool = True):
        super().__init__(message)
        self.timeout = timeout
        self.retryable = retryable


class AIProvider(Protocol):
    name: str
    model: Optional[str]
    #: True when a call can be attempted (key present, endpoint configured).
    configured: bool
    #: True when inputs never leave the machine (local model).
    local: bool

    def complete(self, prompt: str, *, json_mode: bool, timeout_seconds: float, max_output_tokens: int) -> ProviderResult: ...


class StubProvider:
    """Offline, keyless, free: what a fresh clone runs with."""

    name = "stub"
    model = None
    configured = False
    local = True

    def complete(self, prompt: str, *, json_mode: bool, timeout_seconds: float, max_output_tokens: int) -> ProviderResult:
        raise ProviderError("stub provider never answers", retryable=False)


class ScriptedProvider:
    """Deterministic provider for tests: a callable or a list of answers.

    An answer may be a string (returned), a ``ProviderError`` (raised) or any
    other exception instance (raised as-is). Every call is counted.
    """

    local = True

    def __init__(self, answers: Any = None, name: str = "scripted", model: str = "scripted-1", configured: bool = True, latency_fn: Optional[Callable[[], None]] = None):
        self.name = name
        self.model = model
        self.configured = configured
        self.calls = 0
        self.prompts: list[str] = []
        self._answers = answers
        self._latency_fn = latency_fn

    def complete(self, prompt: str, *, json_mode: bool, timeout_seconds: float, max_output_tokens: int) -> ProviderResult:
        self.calls += 1
        self.prompts.append(prompt)
        if self._latency_fn:
            self._latency_fn()
        answer = self._answers
        if callable(answer):
            answer = answer(prompt, json_mode)
        elif isinstance(answer, list):
            answer = answer[min(self.calls - 1, len(answer) - 1)] if answer else None
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            raise ProviderError("scripted provider has no answer", retryable=False)
        if not isinstance(answer, str):
            answer = json.dumps(answer)
        return ProviderResult(text=answer, model=self.model, usage=AIUsage(input_tokens=len(prompt) // 4, output_tokens=len(answer) // 4))


class GeminiProvider:
    """Google Gemini (AI Studio). Header auth only: a key in the query string
    ends up in access logs, proxy logs and exception messages."""

    API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
    local = False

    def __init__(self, api_key: Optional[str], model: str):
        self._api_key = api_key
        self.model = model
        self.name = "gemini"
        self.configured = bool(api_key)

    def complete(self, prompt: str, *, json_mode: bool, timeout_seconds: float, max_output_tokens: int) -> ProviderResult:
        if not self._api_key:
            raise ProviderError("gemini API key not configured", retryable=False)
        generation: dict[str, Any] = {"maxOutputTokens": int(max_output_tokens)}
        if json_mode:
            generation["responseMimeType"] = "application/json"
        payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": generation}
        try:
            response = httpx.post(f"{self.API_ROOT}/{self.model}:generateContent", json=payload, headers={"x-goog-api-key": self._api_key}, timeout=timeout_seconds)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"gemini timeout after {timeout_seconds}s", timeout=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"gemini transport error: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            # Never echo the body: it may quote the request.
            raise ProviderError(f"gemini HTTP {response.status_code}", retryable=response.status_code >= 500 or response.status_code == 429)
        try:
            data = response.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("gemini response had no text candidate", retryable=False) from exc
        meta = data.get("usageMetadata") or {}
        usage = AIUsage(input_tokens=meta.get("promptTokenCount"), output_tokens=meta.get("candidatesTokenCount"))
        return ProviderResult(text=text, model=self.model, usage=usage)


class OllamaProvider:
    """A local model server (http://127.0.0.1:11434 by default). Candidate data
    stays on the machine, which is why it is always allowed for candidate-side
    operations."""

    local = True

    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = "ollama"
        self.configured = bool(model)

    def complete(self, prompt: str, *, json_mode: bool, timeout_seconds: float, max_output_tokens: int) -> ProviderResult:
        payload: dict[str, Any] = {"model": self.model, "prompt": prompt, "stream": False, "options": {"num_predict": int(max_output_tokens)}}
        if json_mode:
            payload["format"] = "json"
        try:
            response = httpx.post(f"{self.base_url}/api/generate", json=payload, timeout=timeout_seconds)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"ollama timeout after {timeout_seconds}s", timeout=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"ollama not reachable: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise ProviderError(f"ollama HTTP {response.status_code}", retryable=response.status_code >= 500)
        try:
            data = response.json()
            text = data["response"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("ollama response had no text", retryable=False) from exc
        usage = AIUsage(input_tokens=data.get("prompt_eval_count"), output_tokens=data.get("eval_count"))
        return ProviderResult(text=text, model=self.model, usage=usage)


KNOWN_PROVIDERS = ("stub", "gemini", "ollama", "scripted")


def build_provider(name: str, model: Optional[str], *, gemini_api_key: Optional[str] = None, gemini_model: str = "gemini-2.0-flash", ollama_url: str = "http://127.0.0.1:11434", ollama_model: str = "llama3.2") -> Optional[AIProvider]:
    """Provider by configured name; ``None`` for an unknown name."""
    key = (name or "stub").strip().lower()
    if key == "stub":
        return StubProvider()
    if key == "gemini":
        return GeminiProvider(gemini_api_key, model or gemini_model)
    if key == "ollama":
        return OllamaProvider(ollama_url, model or ollama_model)
    return None
