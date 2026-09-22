"""Optional AI polish for L2 packages, through the AI Gateway (Phase 8b).

The polisher only ever *rewords* a block that already validates, and every
polished block is validated again by the TruthValidator-backed
PreparationValidator; a rejected rewrite keeps the original text. With the
default configuration nothing is called. Candidate evidence is candidate-side
data (``AIScope.CANDIDATE``): the gateway keeps its cache tenant-private and
only sends it to providers allowed for candidate data.
"""

import logging
from collections import Counter
from typing import Any, Optional, Protocol

from app.ai.models import AIOperation, AIRequest, AIScope, AIStatus, TenantAISettings

logger = logging.getLogger(__name__)

#: Bump when the polish prompt changes (part of the cache key).
POLISH_PROMPT_VERSION = "polish-v2"


class TextPolisher(Protocol):
    name: str
    model: Optional[str]
    calls: int

    def polish(self, text: str, supporting_text: str, purpose: str) -> Optional[str]: ...


class StubPolisher:
    name = "stub"
    model = None

    def __init__(self):
        self.calls = 0

    def polish(self, text: str, supporting_text: str, purpose: str) -> Optional[str]:
        return None

    def metadata(self) -> dict[str, Any]:
        return {"used": False, "provider": None, "calls": 0}


def polish_prompt(text: str, supporting_text: str, purpose: str) -> str:
    return (
        "Rewrite the sentence below for a job application so it reads naturally. "
        "Hard rules: do not add any fact, number, date, employer, title, skill or "
        "achievement that is not in the SUPPORTING EVIDENCE; do not exaggerate; keep "
        "it to one or two sentences; return only the rewritten text.\n\n"
        f"PURPOSE: {purpose}\n\nSENTENCE: {text}\n\nSUPPORTING EVIDENCE:\n{supporting_text[:3000]}"
    )


class GatewayPolisher:
    """Reword-only requests through the gateway; records what happened."""

    def __init__(self, gateway, tenant_id: str, tenant_settings: Optional[TenantAISettings] = None):
        self.gateway = gateway
        self.tenant_id = tenant_id
        self.tenant_settings = tenant_settings or TenantAISettings()
        effective = gateway.effective(self.tenant_settings)
        self.provider_name = effective.provider
        self.model = effective.model
        self.name = f"gateway:{effective.provider}:{effective.model or ''}"
        self.calls = 0
        self.budget = gateway.budget(self.tenant_settings, "preparation")
        self.statuses: Counter = Counter()
        self.cache_hits = 0
        self.fallback_reasons: list[str] = []

    def reset_budget(self) -> None:
        self.budget = self.gateway.budget(self.tenant_settings, "preparation")

    def polish(self, text: str, supporting_text: str, purpose: str) -> Optional[str]:
        self.calls += 1
        request = AIRequest(
            operation=AIOperation.POLISH_TEXT,
            tenant_id=self.tenant_id,
            scope=AIScope.CANDIDATE,
            prompt=polish_prompt(text, supporting_text, purpose),
            input={"text": text, "supporting": supporting_text[:3000], "purpose": purpose},
            prompt_version=POLISH_PROMPT_VERSION,
            max_output_tokens=256,
            reference="preparation",
        )
        response = self.gateway.run(request, tenant=self.tenant_settings, budget=self.budget)
        self.statuses[response.status.value] += 1
        if response.cache_hit:
            self.cache_hits += 1
        if response.status is not AIStatus.OK:
            if response.fallback_reason and response.fallback_reason not in self.fallback_reasons:
                self.fallback_reasons.append(response.fallback_reason[:120])
            return None
        return response.output if isinstance(response.output, str) else None

    def metadata(self) -> dict[str, Any]:
        return {
            "used": self.statuses.get(AIStatus.OK.value, 0) > 0,
            "gateway_version": self.gateway.VERSION,
            "provider": self.provider_name,
            "model": self.model,
            "operation": AIOperation.POLISH_TEXT.value,
            "prompt_version": POLISH_PROMPT_VERSION,
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "by_status": dict(self.statuses),
            "fallback_reasons": list(self.fallback_reasons)[:5],
            "budget": self.budget.snapshot(),
        }


def get_polisher(tenant_id: Optional[str] = None, tenant_settings: Optional[TenantAISettings] = None) -> TextPolisher:
    """The gateway-backed polisher when AI is effectively on for this tenant,
    else the stub (zero calls). Never raises."""
    from app.ai.gateway import get_gateway

    gateway = get_gateway()
    tenant_settings = tenant_settings or TenantAISettings()
    if tenant_id and gateway.effective(tenant_settings).enabled:
        return GatewayPolisher(gateway, tenant_id, tenant_settings)
    return StubPolisher()
