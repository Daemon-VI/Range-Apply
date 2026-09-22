"""Deterministic priority scoring: processing ORDER, never admission.

Inputs are things a scheduler cares about that fit does not: freshness,
deadline urgency, source reliability, execution cost, and (later) learned
priors. Weights are configurable per tenant (``ApplicationPolicy.priority_weights``)
and versioned so a stored score can be reproduced.

Given the same inputs and weights the function returns the same number;
there is no randomness, no clock inside (``now`` is a parameter).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.core.timeutils import ensure_aware, utc_now

WEIGHTS_VERSION = "priority-v1"

DEFAULT_PRIORITY_WEIGHTS: dict[str, float] = {
    "fit": 0.40,
    "freshness": 0.20,
    "deadline": 0.15,
    "source_reliability": 0.10,
    "execution_ease": 0.10,
    "learned_prior": 0.05,
}

#: How dependable each source's postings have been for real, open roles.
SOURCE_RELIABILITY: dict[str, float] = {
    "GREENHOUSE": 90.0,
    "LEVER": 85.0,
    "ASHBY": 85.0,
    "COMPANY_CAREER_PAGE": 70.0,
    "OTHER": 60.0,
}

#: How easy the application is to execute (public board forms are cheap).
EXECUTION_EASE: dict[str, float] = {
    "GREENHOUSE": 85.0,
    "LEVER": 85.0,
    "ASHBY": 80.0,
    "COMPANY_CAREER_PAGE": 50.0,
    "OTHER": 40.0,
}

FRESH_DAYS = 2.0
STALE_DAYS = 30.0


@dataclass
class PriorityInputs:
    fit_score: Optional[int]
    posted_at: Optional[datetime] = None
    first_seen_at: Optional[datetime] = None
    deadline: Optional[datetime] = None
    source: Optional[str] = None
    learned_prior: Optional[float] = None  # 0-100, None = neutral
    now: datetime = field(default_factory=utc_now)


@dataclass
class PriorityResult:
    score: int
    components: dict[str, float]
    weights: dict[str, float]
    weights_version: str = WEIGHTS_VERSION


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def freshness_component(inputs: PriorityInputs) -> float:
    reference = ensure_aware(inputs.posted_at) or ensure_aware(inputs.first_seen_at)
    if reference is None:
        return 50.0
    age_days = max(0.0, (ensure_aware(inputs.now) - reference).total_seconds() / 86400.0)
    if age_days <= FRESH_DAYS:
        return 100.0
    if age_days >= STALE_DAYS:
        return 0.0
    return _clamp(100.0 * (STALE_DAYS - age_days) / (STALE_DAYS - FRESH_DAYS))


def deadline_component(inputs: PriorityInputs) -> float:
    deadline = ensure_aware(inputs.deadline)
    if deadline is None:
        return 20.0
    days_left = (deadline - ensure_aware(inputs.now)).total_seconds() / 86400.0
    if days_left < 0:
        return 0.0
    if days_left <= 3:
        return 100.0
    if days_left <= 7:
        return 70.0
    if days_left <= 14:
        return 40.0
    return 10.0


def compute_priority(inputs: PriorityInputs, weights: Optional[dict[str, float]] = None) -> PriorityResult:
    merged = dict(DEFAULT_PRIORITY_WEIGHTS)
    merged.update({k: float(v) for k, v in (weights or {}).items() if k in merged})
    total = sum(merged.values())
    if total <= 0:
        raise ValueError("Priority weights must sum to a positive number")
    normalized = {k: v / total for k, v in merged.items()}

    source = (inputs.source or "OTHER").upper()
    components = {
        "fit": float(inputs.fit_score if inputs.fit_score is not None else 0),
        "freshness": freshness_component(inputs),
        "deadline": deadline_component(inputs),
        "source_reliability": SOURCE_RELIABILITY.get(source, SOURCE_RELIABILITY["OTHER"]),
        "execution_ease": EXECUTION_EASE.get(source, EXECUTION_EASE["OTHER"]),
        "learned_prior": _clamp(inputs.learned_prior if inputs.learned_prior is not None else 50.0),
    }
    score = sum(components[name] * normalized[name] for name in components)
    return PriorityResult(
        score=int(round(_clamp(score))),
        components={k: round(v, 2) for k, v in components.items()},
        weights={k: round(v, 4) for k, v in normalized.items()},
    )
