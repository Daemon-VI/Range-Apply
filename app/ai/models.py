"""Gateway request/response contract and configuration models."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

#: Bumped when the gateway's request handling, cache layout or accounting changes.
GATEWAY_VERSION = "ai-gateway-v1"


class AIOperation(str, Enum):
    """What a caller asks for. New operations are added here, nowhere else."""

    EXTRACT_JOB_FIELDS = "extract_job_fields"
    POLISH_TEXT = "polish_text"
    CLASSIFY = "classify"
    SUMMARIZE = "summarize"
    #: Blueprint Phase 10: classify an ambiguous inbox signal (category only; never facts or ids).
    CLASSIFY_SIGNAL = "classify_signal"


class AIScope(str, Enum):
    """PII class of the input (blueprint §7): job-side inputs may share a
    cache across tenants; candidate-side inputs are tenant-private and may
    only go to providers allowed for candidate data."""

    JOB = "job"
    CANDIDATE = "candidate"


class AIStatus(str, Enum):
    OK = "OK"
    #: AI is switched off (globally or for the tenant); the caller must use its deterministic path.
    DISABLED = "DISABLED"
    #: Enabled but no usable provider (missing key, provider not reachable at startup).
    UNAVAILABLE = "UNAVAILABLE"
    UNSUPPORTED = "UNSUPPORTED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"
    #: The provider answered but the output failed parsing / schema validation.
    MALFORMED = "MALFORMED"
    #: Refused before any call: the prompt carried credential-like content.
    REFUSED = "REFUSED"


class AIRequest(BaseModel):
    """One unit of AI work. ``input`` is the normalized, cache-relevant input;
    ``prompt`` is what the provider sees (built by the caller from ``input``
    and ``prompt_version``, never containing secrets)."""

    operation: AIOperation
    tenant_id: str = Field(min_length=1)
    scope: AIScope = AIScope.JOB
    prompt: str = Field(min_length=1)
    input: dict[str, Any] = Field(default_factory=dict)
    prompt_version: str = "v1"
    #: A pydantic model class the output must validate against (JSON mode), or None for text.
    schema_model: Optional[type[BaseModel]] = None
    schema_version: str = "v1"
    provider: Optional[str] = None
    model: Optional[str] = None
    timeout_seconds: Optional[float] = None
    max_output_tokens: Optional[int] = None
    cache: bool = True
    #: Free-form correlation ids for accounting (never candidate data).
    application_id: Optional[str] = None
    reference: Optional[str] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)


class AIUsage(BaseModel):
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    estimated_cost_usd: Optional[float] = None


class AIResponse(BaseModel):
    status: AIStatus
    operation: AIOperation
    provider: Optional[str] = None
    model: Optional[str] = None
    #: Parsed output: a dict when a schema was requested, else the text.
    output: Optional[Any] = None
    cache_hit: bool = False
    cache_key: Optional[str] = None
    latency_ms: int = 0
    usage: AIUsage = Field(default_factory=AIUsage)
    #: Why the caller must fall back (always set unless status is OK).
    fallback_reason: Optional[str] = None
    gateway_version: str = GATEWAY_VERSION
    prompt_version: str = "v1"

    @property
    def ok(self) -> bool:
        return self.status is AIStatus.OK

    def metadata(self) -> dict[str, Any]:
        """Safe-to-persist description of this call (no prompt, no output)."""
        return {
            "status": self.status.value,
            "operation": self.operation.value,
            "provider": self.provider,
            "model": self.model,
            "cache_hit": self.cache_hit,
            "latency_ms": self.latency_ms,
            "fallback_reason": self.fallback_reason,
            "gateway_version": self.gateway_version,
            "prompt_version": self.prompt_version,
        }


class ProviderResult(BaseModel):
    text: str
    usage: AIUsage = Field(default_factory=AIUsage)
    model: Optional[str] = None


class TenantAISettings(BaseModel):
    """Per-tenant AI settings stored on the application policy. A tenant can
    only narrow what the global configuration allows; it can never turn AI on
    while ``AI_ENABLED`` is false, nor pick a provider that is not configured."""

    enabled: bool = False
    provider: Optional[str] = None
    model: Optional[str] = None
    max_calls_per_run: Optional[int] = Field(default=None, ge=0)
    max_output_tokens: Optional[int] = Field(default=None, ge=1)
    timeout_seconds: Optional[float] = Field(default=None, gt=0, le=300)
    cache_enabled: Optional[bool] = None


class EffectiveAIConfig(BaseModel):
    """What actually applies to one tenant right now (global ∧ tenant)."""

    enabled: bool
    provider: str
    model: Optional[str] = None
    timeout_seconds: float
    max_calls_per_run: int
    max_output_tokens: int
    cache_enabled: bool
    candidate_data_providers: list[str] = Field(default_factory=list)
    global_enabled: bool
    tenant_enabled: bool
    provider_configured: bool
    reason: Optional[str] = None
    gateway_version: str = GATEWAY_VERSION


class AIUsageRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    operation: str
    scope: str
    provider: Optional[str] = None
    model: Optional[str] = None
    status: str
    cache_hit: bool
    latency_ms: int
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    estimated_cost_usd: Optional[float] = None
    fallback_reason: Optional[str] = None
    prompt_version: str
    gateway_version: str
    reference: Optional[str] = None
    created_at: Optional[datetime] = None
