"""The one place a model is called.

Flow (blueprint §7): RULES (enabled? provider allowed for this PII class?
prompt free of credentials?) → CACHE → BUDGET → PROVIDER → VALIDATION →
CACHE → ACCOUNTING. Every step that stops the call returns a controlled
:class:`AIResponse` with a ``fallback_reason``; nothing raises into the
caller's pipeline.
"""

import json
import logging
import re
import threading
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel, ValidationError

from app.ai.budget import CallBudget
from app.ai.cache import AICache, MemoryCache, cache_key
from app.ai.models import (
    GATEWAY_VERSION,
    AIRequest,
    AIResponse,
    AIScope,
    AIStatus,
    AIUsage,
    EffectiveAIConfig,
    TenantAISettings,
)
from app.ai.providers import KNOWN_PROVIDERS, AIProvider, ProviderError, build_provider
from app.config import settings

logger = logging.getLogger(__name__)

#: Credential-shaped content is refused before any provider sees it.
_CREDENTIAL_PATTERNS = re.compile(
    r"(?i)(bearer\s+[a-z0-9\-_.]{16,}|api[_-]?key\s*[:=]\s*\S{12,}|x-goog-api-key|authorization\s*:|cookie\s*:|set-cookie|password\s*[:=]\s*\S+|sk-[a-z0-9]{20,}|AIza[0-9A-Za-z\-_]{30,})"
)


class GlobalAIConfig(BaseModel):
    """Process-wide configuration from settings (env). The only place API
    keys live; they never reach tenant settings, the DB, logs or prompts."""

    enabled: bool = False
    provider: str = "stub"
    model: Optional[str] = None
    timeout_seconds: float = 20.0
    max_calls_per_run: int = 50
    max_output_tokens: int = 1024
    cache_enabled: bool = True
    cache_dir: Optional[str] = None
    candidate_data_providers: list[str] = ["ollama", "stub"]
    persist_usage: bool = True

    @classmethod
    def from_settings(cls) -> "GlobalAIConfig":
        provider = (settings.ai_provider or "stub").strip().lower()
        # Legacy Phase 2 knob: LLM_PROVIDER=gemini still selects Gemini, but only
        # AI_ENABLED=true turns AI on. Nothing becomes active by accident.
        if provider == "stub" and (settings.llm_provider or "").strip().lower() == "gemini":
            provider = "gemini"
        return cls(
            enabled=bool(settings.ai_enabled),
            provider=provider,
            model=settings.ai_model or None,
            timeout_seconds=float(settings.ai_timeout_seconds),
            max_calls_per_run=int(settings.ai_max_calls_per_run),
            max_output_tokens=int(settings.ai_max_output_tokens),
            cache_enabled=bool(settings.ai_cache_enabled),
            cache_dir=settings.ai_cache_dir,
            candidate_data_providers=[p.strip().lower() for p in (settings.ai_candidate_data_providers or "").split(",") if p.strip()],
            persist_usage=bool(settings.ai_usage_persist),
        )


class UsageSink:
    """In-memory accounting (always) plus optional durable rows."""

    def __init__(self, persist: bool = False, keep: int = 500):
        self.persist = persist
        self.recent: deque = deque(maxlen=keep)
        self.counts: Counter = Counter()
        self.calls = 0
        self.cache_hits = 0
        self.latency_ms_total = 0
        self.by_operation: Counter = Counter()
        self.lock = threading.Lock()

    def record(self, request: AIRequest, response: AIResponse) -> None:
        with self.lock:
            self.counts[response.status.value] += 1
            self.by_operation[request.operation.value] += 1
            if response.status is AIStatus.OK and not response.cache_hit:
                self.calls += 1
            if response.cache_hit:
                self.cache_hits += 1
            self.latency_ms_total += response.latency_ms
            self.recent.appendleft({"tenant_id": request.tenant_id, "scope": request.scope.value, "reference": request.reference, **response.metadata()})
        if self.persist:
            self._persist(request, response)

    def _persist(self, request: AIRequest, response: AIResponse) -> None:
        try:
            from app.ai.database.models import AIUsageRow
            from app.database import get_session_factory

            session = get_session_factory()()
            try:
                session.add(
                    AIUsageRow(
                        tenant_id=request.tenant_id,
                        operation=request.operation.value,
                        scope=request.scope.value,
                        provider=response.provider,
                        model=response.model,
                        status=response.status.value,
                        cache_hit=response.cache_hit,
                        latency_ms=response.latency_ms,
                        input_tokens=response.usage.input_tokens,
                        output_tokens=response.usage.output_tokens,
                        estimated_cost_usd=response.usage.estimated_cost_usd,
                        fallback_reason=(response.fallback_reason or "")[:256] or None,
                        prompt_version=response.prompt_version,
                        gateway_version=response.gateway_version,
                        reference=(request.reference or request.application_id or "")[:128] or None,
                    )
                )
                session.commit()
            finally:
                session.close()
        except Exception as exc:  # noqa: BLE001 - accounting must never break the caller
            logger.debug("AI usage row not persisted: %s", type(exc).__name__)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            total = sum(self.counts.values())
            return {
                "requests": total,
                "provider_calls": self.calls,
                "cache_hits": self.cache_hits,
                "by_status": dict(self.counts),
                "by_operation": dict(self.by_operation),
                "avg_latency_ms": round(self.latency_ms_total / total, 1) if total else 0.0,
            }

    def reset(self) -> None:
        with self.lock:
            self.recent.clear()
            self.counts.clear()
            self.by_operation.clear()
            self.calls = self.cache_hits = self.latency_ms_total = 0


class AIGateway:
    VERSION = GATEWAY_VERSION

    def __init__(
        self,
        config: Optional[GlobalAIConfig] = None,
        provider: Optional[AIProvider] = None,
        cache: Optional[AICache] = None,
        sink: Optional[UsageSink] = None,
        provider_factory: Optional[Callable[[str, Optional[str]], Optional[AIProvider]]] = None,
        clock: Callable[[], float] = time.perf_counter,
    ):
        self.config = config or GlobalAIConfig.from_settings()
        self._provider_factory = provider_factory or self._default_factory
        self._provider = provider
        self._providers: dict[tuple[str, Optional[str]], AIProvider] = {}
        if cache is not None:
            self.cache = cache
        elif self.config.cache_dir:
            self.cache = AICache(Path(self.config.cache_dir))
        else:
            self.cache = MemoryCache()
        self.sink = sink or UsageSink(persist=self.config.persist_usage)
        self.clock = clock

    # ------------------------------------------------------------ config

    def _default_factory(self, name: str, model: Optional[str]) -> Optional[AIProvider]:
        return build_provider(name, model, gemini_api_key=settings.gemini_api_key, gemini_model=settings.gemini_model, ollama_url=settings.ai_ollama_url, ollama_model=settings.ai_ollama_model)

    def provider_for(self, name: str, model: Optional[str]) -> Optional[AIProvider]:
        if self._provider is not None and (name == self._provider.name or name == self.config.provider):
            return self._provider
        key = (name, model)
        if key not in self._providers:
            built = self._provider_factory(name, model)
            if built is None:
                return None
            self._providers[key] = built
        return self._providers[key]

    def effective(self, tenant: Optional[TenantAISettings] = None) -> EffectiveAIConfig:
        """global ∧ tenant. A tenant can narrow, never widen.

        ``tenant=None`` means a job-side (shared, no candidate data) caller
        such as discovery: only the global switch applies. Candidate-side
        callers always pass the tenant's policy settings, whose ``enabled``
        defaults to False.
        """
        tenant = tenant if tenant is not None else TenantAISettings(enabled=True)
        cfg = self.config
        provider_name = (tenant.provider or cfg.provider or "stub").strip().lower()
        if tenant.provider and tenant.provider.strip().lower() != cfg.provider and tenant.provider.strip().lower() not in ("ollama", "stub"):
            # Only the globally configured provider (or a local one) may be chosen per tenant.
            provider_name = cfg.provider
        provider = self.provider_for(provider_name, tenant.model or cfg.model)
        configured = bool(provider is not None and provider.configured)
        enabled = bool(cfg.enabled and tenant.enabled and configured)
        reason = None
        if not cfg.enabled:
            reason = "AI_ENABLED is false"
        elif not tenant.enabled:
            reason = "AI is off for this tenant (policy.ai_settings.enabled)"
        elif provider is None:
            reason = f"unsupported provider '{provider_name}'"
        elif not configured:
            reason = f"provider '{provider_name}' is not configured (missing key or model)"
        max_calls = cfg.max_calls_per_run if tenant.max_calls_per_run is None else min(cfg.max_calls_per_run, tenant.max_calls_per_run)
        max_tokens = cfg.max_output_tokens if tenant.max_output_tokens is None else min(cfg.max_output_tokens, tenant.max_output_tokens)
        timeout = cfg.timeout_seconds if tenant.timeout_seconds is None else min(cfg.timeout_seconds, tenant.timeout_seconds)
        cache_enabled = cfg.cache_enabled if tenant.cache_enabled is None else (cfg.cache_enabled and tenant.cache_enabled)
        return EffectiveAIConfig(
            enabled=enabled,
            provider=provider_name,
            model=(provider.model if provider is not None else None) or tenant.model or cfg.model,
            timeout_seconds=timeout,
            max_calls_per_run=max_calls,
            max_output_tokens=max_tokens,
            cache_enabled=cache_enabled,
            candidate_data_providers=list(cfg.candidate_data_providers),
            global_enabled=cfg.enabled,
            tenant_enabled=tenant.enabled,
            provider_configured=configured,
            reason=reason,
        )

    def budget(self, tenant: Optional[TenantAISettings] = None, name: str = "run") -> CallBudget:
        return CallBudget(self.effective(tenant).max_calls_per_run, name)

    # --------------------------------------------------------------- run

    def run(self, request: AIRequest, tenant: Optional[TenantAISettings] = None, budget: Optional[CallBudget] = None) -> AIResponse:
        started = self.clock()
        cfg = self.effective(tenant)

        def finish(status: AIStatus, *, output: Any = None, provider: Optional[str] = None, model: Optional[str] = None, cache_hit: bool = False, key: Optional[str] = None, usage: Optional[AIUsage] = None, reason: Optional[str] = None) -> AIResponse:
            response = AIResponse(
                status=status,
                operation=request.operation,
                provider=provider,
                model=model,
                output=output,
                cache_hit=cache_hit,
                cache_key=key,
                latency_ms=int((self.clock() - started) * 1000),
                usage=usage or AIUsage(),
                fallback_reason=None if status is AIStatus.OK else (reason or status.value.lower()),
                prompt_version=request.prompt_version,
            )
            self.sink.record(request, response)
            return response

        # ---- rules -----------------------------------------------------
        if not cfg.enabled:
            if not (cfg.global_enabled and cfg.tenant_enabled):
                status = AIStatus.DISABLED
            elif cfg.provider not in KNOWN_PROVIDERS and self.provider_for(cfg.provider, cfg.model) is None:
                status = AIStatus.UNSUPPORTED
            else:
                status = AIStatus.UNAVAILABLE
            return finish(status, provider=cfg.provider, model=cfg.model, reason=cfg.reason)
        provider_name = (request.provider or cfg.provider).strip().lower()
        provider = self.provider_for(provider_name, request.model or cfg.model)
        if provider is None:
            return finish(AIStatus.UNSUPPORTED, provider=provider_name, reason=f"unsupported provider '{provider_name}'")
        if not provider.configured:
            return finish(AIStatus.UNAVAILABLE, provider=provider.name, model=provider.model, reason=f"provider '{provider.name}' is not configured")
        if request.scope is AIScope.CANDIDATE and not provider.local and provider.name not in cfg.candidate_data_providers:
            return finish(AIStatus.REFUSED, provider=provider.name, model=provider.model, reason=f"provider '{provider.name}' is not allowed for candidate data (AI_CANDIDATE_DATA_PROVIDERS)")
        if self._prompt_has_credentials(request.prompt):
            return finish(AIStatus.REFUSED, provider=provider.name, model=provider.model, reason="prompt contains credential-like content")

        key = cache_key(operation=request.operation.value, prompt_version=request.prompt_version, schema_version=request.schema_version, provider=provider.name, model=provider.model, scope=request.scope, tenant_id=request.tenant_id, input_data=request.input)

        # ---- cache -----------------------------------------------------
        use_cache = cfg.cache_enabled and request.cache
        if use_cache:
            entry = self.cache.get(key)
            if entry is not None:
                output = entry.get("output")
                parsed = self._validate(output, request)
                if parsed is not None:
                    usage = AIUsage(**(entry.get("usage") or {}))
                    return finish(AIStatus.OK, output=parsed, provider=provider.name, model=entry.get("model") or provider.model, cache_hit=True, key=key, usage=usage)
                logger.debug("AI cache entry %s failed validation; refetching", key[:12])

        # ---- budget ----------------------------------------------------
        if budget is not None and not budget.take(request.operation.value):
            return finish(AIStatus.BUDGET_EXHAUSTED, provider=provider.name, model=provider.model, key=key, reason=f"budget '{budget.name}' exhausted ({budget.max_calls} calls)")

        # ---- provider --------------------------------------------------
        timeout = min(request.timeout_seconds, cfg.timeout_seconds) if request.timeout_seconds else cfg.timeout_seconds
        max_tokens = min(request.max_output_tokens, cfg.max_output_tokens) if request.max_output_tokens else cfg.max_output_tokens
        try:
            result = provider.complete(request.prompt, json_mode=request.schema_model is not None, timeout_seconds=timeout, max_output_tokens=max_tokens)
        except ProviderError as exc:
            status = AIStatus.TIMEOUT if exc.timeout else AIStatus.ERROR
            logger.warning("AI %s via %s failed: %s", request.operation.value, provider.name, type(exc).__name__)
            return finish(status, provider=provider.name, model=provider.model, key=key, reason=str(exc)[:200])
        except Exception as exc:  # noqa: BLE001 - a provider bug is a fallback, never an outage
            logger.warning("AI %s via %s crashed: %s", request.operation.value, provider.name, type(exc).__name__)
            return finish(AIStatus.ERROR, provider=provider.name, model=provider.model, key=key, reason=f"provider crashed: {type(exc).__name__}")

        # ---- validation ------------------------------------------------
        parsed = self._validate(result.text, request)
        if parsed is None:
            return finish(AIStatus.MALFORMED, provider=provider.name, model=result.model or provider.model, key=key, usage=result.usage, reason="provider output failed parsing/schema validation")

        # ---- cache + accounting ---------------------------------------
        if use_cache:
            self.cache.put(key, output=parsed, provider=provider.name, model=result.model or provider.model, operation=request.operation.value, prompt_version=request.prompt_version, usage=result.usage.model_dump())
        return finish(AIStatus.OK, output=parsed, provider=provider.name, model=result.model or provider.model, key=key, usage=result.usage)

    # ----------------------------------------------------------- helpers

    @staticmethod
    def _prompt_has_credentials(prompt: str) -> bool:
        if _CREDENTIAL_PATTERNS.search(prompt):
            return True
        for secret in (settings.api_key, settings.gemini_api_key, settings.firecrawl_api_key):
            if secret and len(secret) >= 8 and secret in prompt:
                return True
        return False

    @staticmethod
    def _validate(raw: Any, request: AIRequest) -> Optional[Any]:
        """Parsed output or None. Text outputs must be non-empty strings;
        structured outputs must be JSON that validates against the schema."""
        if request.schema_model is None:
            if not isinstance(raw, str):
                return None
            text = raw.strip()
            return text or None
        data = raw
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith("```"):
                text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
            try:
                data = json.loads(text)
            except ValueError:
                return None
        if not isinstance(data, dict):
            return None
        try:
            return request.schema_model.model_validate(data).model_dump(mode="json")
        except ValidationError:
            return None

    def stats(self) -> dict[str, Any]:
        return {"gateway_version": self.VERSION, **self.sink.snapshot(), "cache": {"hits": self.cache.hits, "misses": self.cache.misses, "writes": self.cache.writes}}


_gateway: Optional[AIGateway] = None
_lock = threading.Lock()


def get_gateway() -> AIGateway:
    """Process-wide gateway built from settings (lazy)."""
    global _gateway
    if _gateway is None:
        with _lock:
            if _gateway is None:
                _gateway = AIGateway()
    return _gateway


def reset_gateway(gateway: Optional[AIGateway] = None) -> None:
    """Replace (or drop) the process-wide gateway; tests and config reloads."""
    global _gateway
    with _lock:
        _gateway = gateway
