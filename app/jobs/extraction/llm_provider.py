"""LLM extraction provider for the discovery fallback.

Since Blueprint Phase 8b the only real implementation is
:class:`GatewayLLMProvider`, a thin adapter over the AI Gateway
(``app.ai``): the gateway owns the provider (Gemini, a local Ollama model,
...), the versioned on-disk cache, budgets and accounting. The
``LLMProvider`` contract the extractor calls is unchanged, and the offline
:class:`StubProvider` remains the default so a fresh clone runs with no key
and no outbound traffic.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from app.ai.budget import CallBudget
from app.ai.models import AIOperation, AIRequest, AIScope, AIStatus
from app.config import settings

logger = logging.getLogger(__name__)

#: Bump when the extraction prompt or the expected fields change (cache key).
EXTRACTION_PROMPT_VERSION = "extract-v2"


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


class ExtractedJobFields(BaseModel):
    """Schema the gateway validates AI output against. Every field is optional
    and typed loosely; the extractor still checks enum membership and only
    fills fields deterministic extraction left UNKNOWN."""

    employment_type: Optional[str] = None
    remote_type: Optional[str] = None
    experience_level: Optional[str] = None
    graduation_minimum_year: Optional[int] = Field(default=None, ge=1990, le=2100)
    graduation_maximum_year: Optional[int] = Field(default=None, ge=1990, le=2100)
    salary_text: Optional[str] = Field(default=None, max_length=200)


def extraction_prompt(title: str, content: str) -> str:
    return f"""You are a job description parser. Extract structured fields from the job description below.
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


class GatewayLLMProvider(LLMProvider):
    """Job-side extraction through the AI Gateway (shared cache, global switch)."""

    def __init__(self, gateway=None, tenant_id: Optional[str] = None, budget: Optional[CallBudget] = None):
        from app.ai.gateway import get_gateway

        self.gateway = gateway or get_gateway()
        self.tenant_id = tenant_id or settings.default_tenant_id
        self.budget = budget or self.gateway.budget(None, "discovery-run")
        effective = self.gateway.effective(None)
        self.provider_name = effective.provider
        self.model = effective.model
        self.name = f"gateway:{effective.provider}:{effective.model or ''}"
        # Counters the discovery run reads (same names the old cache exposed).
        self.hits = 0
        self.misses = 0
        self.last_metadata: Optional[dict[str, Any]] = None

    def new_budget(self) -> None:
        self.budget = self.gateway.budget(None, "discovery-run")

    def _run(self, title: str, content: str) -> Dict[str, Any]:
        request = AIRequest(
            operation=AIOperation.EXTRACT_JOB_FIELDS,
            tenant_id=self.tenant_id,
            scope=AIScope.JOB,
            prompt=extraction_prompt(title, content),
            input={"title": title.strip().lower(), "content": " ".join(content[:2500].split())},
            prompt_version=EXTRACTION_PROMPT_VERSION,
            schema_model=ExtractedJobFields,
            schema_version="extracted-job-fields-v1",
            reference="discovery",
        )
        response = self.gateway.run(request, tenant=None, budget=self.budget)
        self.last_metadata = response.metadata()
        if response.cache_hit:
            self.hits += 1
        else:
            self.misses += 1
        if response.status is not AIStatus.OK or not isinstance(response.output, dict):
            return {}
        return {k: v for k, v in response.output.items() if v is not None}

    async def extract_ambiguous_fields(self, title: str, content: str) -> Dict[str, Any]:
        # The gateway is synchronous (httpx); keep the event loop free.
        return await asyncio.to_thread(self._run, title, content)


def get_llm_provider() -> LLMProvider:
    """Factory: the gateway-backed provider when AI is on, else the stub.

    ``AI_ENABLED=false`` (the default) always yields the stub, whatever
    ``LLM_PROVIDER`` / ``AI_PROVIDER`` say: no key, no outbound traffic.
    """
    from app.ai.gateway import get_gateway
    from app.config import settings

    # Discovery extraction needs its own opt-in (AI_JOB_EXTRACTION_ENABLED): with AI on
    # for the candidate's answers, it otherwise spent the free quota on every posting.
    if not settings.ai_job_extraction_enabled:
        return StubProvider()
    gateway = get_gateway()
    if gateway.effective(None).enabled:
        return GatewayLLMProvider(gateway)
    return StubProvider()
