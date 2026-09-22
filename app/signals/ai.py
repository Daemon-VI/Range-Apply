"""Optional AI classification of ambiguous signals — through the Phase 8b
gateway only (operation ``CLASSIFY_SIGNAL``).

Used solely when the deterministic classifier is not confident. Off by
default; with AI disabled the caller never reaches this module's gateway
call. The model returns a category from the fixed enum and nothing else is
taken from its output: no identifiers, no application relationship, no
candidate facts. Its classification is recorded with ``ClassificationSource.AI``
and therefore never stronger than WEAK evidence.
"""

from collections import Counter
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.ai.models import AIOperation, AIRequest, AIScope, AIStatus, TenantAISettings
from app.signals.models import (
    CLASSIFIER_VERSION,
    Classification,
    ClassificationSource,
    Confidence,
    SignalCategory,
)

#: Bump when the prompt changes (part of the cache key).
CLASSIFY_PROMPT_VERSION = "classify-signal-v1"
#: What the model may see: a bounded excerpt, never the whole thread.
PROMPT_EXCERPT_CHARS = 1500

_AI_CATEGORIES = [c.value for c in SignalCategory if c not in (SignalCategory.EXECUTION_RESULT,)]


class SignalClassificationOutput(BaseModel):
    category: str = Field(min_length=1, max_length=40)
    confidence: str = Field(default="LOW", max_length=8)
    reason: Optional[str] = Field(default=None, max_length=300)


def classify_prompt(subject: str, excerpt: str, sender_domain: Optional[str]) -> str:
    return (
        "Classify this message received by a job applicant. Answer with JSON only: "
        '{"category": <one of the categories>, "confidence": "HIGH"|"MEDIUM"|"LOW", "reason": <short>}.\n'
        f"Categories: {', '.join(_AI_CATEGORIES)}.\n"
        "Rules: choose UNKNOWN when unsure; never invent identifiers; do not summarise the person.\n\n"
        f"SENDER DOMAIN: {sender_domain or 'unknown'}\nSUBJECT: {subject[:300]}\n\nMESSAGE:\n{excerpt[:PROMPT_EXCERPT_CHARS]}"
    )


class GatewaySignalClassifier:
    """One instance per processing batch; owns the batch's call budget."""

    def __init__(self, gateway, tenant_id: str, tenant_settings: Optional[TenantAISettings] = None):
        self.gateway = gateway
        self.tenant_id = tenant_id
        self.tenant_settings = tenant_settings or TenantAISettings()
        self.effective = gateway.effective(self.tenant_settings)
        self.budget = gateway.budget(self.tenant_settings, "signals")
        self.statuses: Counter = Counter()
        self.calls = 0
        self.cache_hits = 0

    @property
    def enabled(self) -> bool:
        return bool(self.effective.enabled)

    def classify(self, subject: Optional[str], excerpt: Optional[str], sender_domain: Optional[str], reference: Optional[str] = None) -> tuple[Optional[Classification], dict[str, Any]]:
        """``(classification or None, gateway metadata)``. ``None`` = fall back."""
        self.calls += 1
        request = AIRequest(
            operation=AIOperation.CLASSIFY_SIGNAL,
            tenant_id=self.tenant_id,
            scope=AIScope.CANDIDATE,
            prompt=classify_prompt(subject or "", excerpt or "", sender_domain),
            input={"subject": (subject or "")[:300], "excerpt": (excerpt or "")[:PROMPT_EXCERPT_CHARS], "sender_domain": sender_domain or ""},
            prompt_version=CLASSIFY_PROMPT_VERSION,
            schema_model=SignalClassificationOutput,
            schema_version="v1",
            max_output_tokens=128,
            reference=reference or "signal",
        )
        response = self.gateway.run(request, tenant=self.tenant_settings, budget=self.budget)
        self.statuses[response.status.value] += 1
        if response.cache_hit:
            self.cache_hits += 1
        meta = {**response.metadata(), "scope": AIScope.CANDIDATE.value}
        if response.status is not AIStatus.OK or not isinstance(response.output, dict):
            return None, meta
        raw = str(response.output.get("category", "")).strip().upper()
        if raw not in _AI_CATEGORIES:
            meta["fallback_reason"] = f"category '{raw[:40]}' is not in the enum"
            meta["status"] = AIStatus.MALFORMED.value
            return None, meta
        confidence = Confidence.MEDIUM if str(response.output.get("confidence", "")).upper() in ("HIGH", "MEDIUM") else Confidence.LOW
        return (
            Classification(
                category=SignalCategory(raw),
                confidence=confidence,  # AI is never HIGH: it is a signal, not truth
                source=ClassificationSource.AI,
                classifier_version=f"{CLASSIFIER_VERSION}+{CLASSIFY_PROMPT_VERSION}",
                matched_rules=[],
                ai={k: v for k, v in meta.items() if k in ("provider", "model", "cache_hit", "latency_ms", "gateway_version", "prompt_version")},
            ),
            meta,
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "gateway_version": self.gateway.VERSION,
            "operation": AIOperation.CLASSIFY_SIGNAL.value,
            "provider": self.effective.provider,
            "model": self.effective.model,
            "prompt_version": CLASSIFY_PROMPT_VERSION,
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "by_status": dict(self.statuses),
            "budget": self.budget.snapshot(),
        }


def get_signal_classifier(tenant_id: str, tenant_settings: Optional[TenantAISettings] = None) -> Optional[GatewaySignalClassifier]:
    """A gateway-backed classifier when AI is effectively on for the tenant, else ``None``."""
    from app.ai.gateway import get_gateway

    gateway = get_gateway()
    settings_ = tenant_settings or TenantAISettings()
    if gateway.effective(settings_).enabled:
        return GatewaySignalClassifier(gateway, tenant_id, settings_)
    return None


__all__ = ["CLASSIFY_PROMPT_VERSION", "GatewaySignalClassifier", "SignalClassificationOutput", "classify_prompt", "get_signal_classifier"]
