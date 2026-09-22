"""The AI Gateway (Blueprint Phase 8b, §7).

Every model call in CareerOS goes through :class:`app.ai.gateway.AIGateway`.
AI is optional by construction: with the defaults nothing is enabled, every
caller receives a controlled ``DISABLED`` response and falls back to its
deterministic path. The gateway never creates candidate facts; the Evidence
Graph and the TruthValidator stay the only sources of truth.
"""

from app.ai.gateway import AIGateway, get_gateway, reset_gateway
from app.ai.models import (
    AIOperation,
    AIRequest,
    AIResponse,
    AIScope,
    AIStatus,
    EffectiveAIConfig,
    TenantAISettings,
)

__all__ = [
    "AIGateway",
    "AIOperation",
    "AIRequest",
    "AIResponse",
    "AIScope",
    "AIStatus",
    "EffectiveAIConfig",
    "TenantAISettings",
    "get_gateway",
    "reset_gateway",
]
