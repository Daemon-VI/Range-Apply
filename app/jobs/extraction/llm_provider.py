"""LLM Provider abstraction for structured extraction fallback.

Free-tier posture:

* Deterministic extraction runs first; a provider is consulted only for fields
  it could not resolve (see ``LLMFallbackExtractor``).
* Every provider sits behind :class:`LLMProvider`, so switching between free
  tiers (Google AI Studio, Groq, OpenRouter, ...) never touches business logic.
* Results are cached by content hash, so re-ingesting an unchanged posting
  costs zero API calls. The cache is a plain directory of JSON files: no Redis,
  no hosted cache, works on any free host with a writable disk.
"""

import hashlib
import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from app.config import PROJECT_ROOT, settings

logger = logging.getLogger(__name__)

CACHE_DIR = PROJECT_ROOT / ".cache" / "llm"


class LLMProvider(ABC):
    """Abstract interface for LLM extraction providers."""

    #: Identifies the provider in cache keys, so switching models invalidates.
    name: str = "abstract"

    @abstractmethod
    async def extract_ambiguous_fields(self, title: str, content: str) -> Dict[str, Any]:
        """Extracts structured fields from job posting text for ambiguous cases.

        Returns a dict that may contain:
        - employment_type: str
        - remote_type: str
        - experience_level: str
        - graduation_minimum_year / graduation_maximum_year: int
        - salary_text: str
        """
        pass


class StubProvider(LLMProvider):
    """Offline stub provider for unit tests and default keyless operation."""

    name = "stub"

    async def extract_ambiguous_fields(self, title: str, content: str) -> Dict[str, Any]:
        logger.debug("StubProvider called for '%s' — returning empty fallback", title)
        return {}


class GeminiProvider(LLMProvider):
    """Google Gemini provider for semantic extraction fallback.

    The model id is configurable (``GEMINI_MODEL``) because Google retires
    model aliases on its own schedule; pinning one in code guarantees a silent
    breakage later.
    """

    API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: float = 15.0,
        model: Optional[str] = None,
    ):
        self.api_key = api_key or settings.gemini_api_key
        self.timeout = timeout
        self.model = model or settings.gemini_model
        self.name = f"gemini:{self.model}"

    async def extract_ambiguous_fields(self, title: str, content: str) -> Dict[str, Any]:
        if not self.api_key:
            logger.warning("Gemini API key not configured, falling back to empty extraction")
            return {}

        prompt = f"""You are a job description parser. Extract structured fields from the job description below.
Return ONLY valid JSON matching this schema:
{{
  "employment_type": "INTERNSHIP" | "FULL_TIME" | "PART_TIME" | "CONTRACT" | "UNKNOWN",
  "remote_type": "REMOTE" | "HYBRID" | "ON_SITE" | "UNKNOWN",
  "experience_level": "INTERN" | "ENTRY_LEVEL" | "JUNIOR" | "MID" | "SENIOR" | "UNKNOWN",
  "graduation_minimum_year": null | integer,
  "graduation_maximum_year": null | integer,
  "salary_text": null | string
}}

Job Title: {title}
Job Description (first 2500 chars):
{content[:2500]}
"""

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.API_ROOT}/{self.model}:generateContent",
                    json=payload,
                    # Header auth: a key in the query string ends up in access
                    # logs, proxy logs and exception messages.
                    headers={"x-goog-api-key": self.api_key},
                )
                response.raise_for_status()
                data = response.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
        except Exception as e:  # noqa: BLE001 - never let extraction break ingestion
            # self.api_key is never interpolated into the message.
            logger.warning("Gemini extraction failed for '%s': %s", title, type(e).__name__)
            return {}


class CachingLLMProvider(LLMProvider):
    """Wraps a provider with a content-hash keyed on-disk cache.

    Re-running discovery over a board whose postings have not changed produces
    identical prompts; without this, every run would repay the same API quota.
    Cache misses degrade to a normal call, and any cache I/O error is non-fatal.
    """

    def __init__(self, inner: LLMProvider, cache_dir: Optional[Path] = None):
        self.inner = inner
        self.name = f"cached:{inner.name}"
        self.cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR

    def _key(self, title: str, content: str) -> str:
        digest = hashlib.sha256()
        digest.update(self.inner.name.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(title.strip().lower().encode("utf-8"))
        digest.update(b"\x00")
        digest.update(" ".join(content.split()).encode("utf-8"))
        return digest.hexdigest()

    async def extract_ambiguous_fields(self, title: str, content: str) -> Dict[str, Any]:
        key = self._key(title, content)
        path = self.cache_dir / f"{key}.json"

        try:
            if path.exists():
                logger.debug("LLM cache hit for '%s'", title)
                return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.debug("Unreadable LLM cache entry %s; ignoring", path.name)

        result = await self.inner.extract_ambiguous_fields(title, content)

        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result), encoding="utf-8")
        except OSError:
            logger.debug("Could not write LLM cache entry; continuing without cache")

        return result


def get_llm_provider() -> LLMProvider:
    """Factory selecting the provider from configuration.

    Defaults to the offline stub so a fresh clone runs with no API key and no
    outbound LLM traffic.
    """
    if settings.llm_provider == "gemini" and settings.gemini_api_key:
        return CachingLLMProvider(GeminiProvider())
    return StubProvider()
