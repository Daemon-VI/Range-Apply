"""Outcome model: what a signal says happened, and how the current status is
derived from the append-only event history (Blueprint Phase 10 §6, §11, §17).

Derivation rules (``OUTCOME_RULES_VERSION``), applied to the non-retracted,
non-superseded events of one application ordered by ``(event_at, sequence)``:

1. Only STRONG events may set a *terminal* outcome (REJECTED / WITHDRAWN /
   CLOSED_WITHOUT_APPLICATION). STRONG or MODERATE events may set a
   *progress* outcome. WEAK events never set the status; they appear as
   ``provisional`` and flag the application for review.
2. The progress status is the highest stage reached, not the latest event:
   employers send things out of order and a late confirmation must not
   demote an interview.
3. A terminal event after progress is a normal ending (REJECTED after an
   interview; WITHDRAWN after an invitation).
4. Conflicts, each of which makes the status NEEDS_REVIEW and is listed:
   progress evidence *after* a terminal event; two different terminal
   outcomes. A terminal event that only has WEAK / MODERATE evidence is not
   a conflict: it is shown as ``provisional`` and flags review, while the
   established status stands.
5. A NEEDS_REVIEW event (failed verification, a rejected classification)
   flags review without changing the status derived from the other events.
"""

from typing import Iterable, Optional

from app.execution.models import VerificationStatus
from app.signals.models import (
    OUTCOME_RULES_VERSION,
    ClassificationSource,
    Confidence,
    DerivedOutcome,
    EvidenceStrength,
    OutcomeKind,
    SignalCategory,
)

#: What each classification says happened. ``None`` = nothing to record.
OUTCOME_FOR_CATEGORY: dict[SignalCategory, Optional[OutcomeKind]] = {
    SignalCategory.APPLICATION_CONFIRMATION: OutcomeKind.APPLICATION_RECEIVED,
    SignalCategory.APPLICATION_RECEIVED: OutcomeKind.APPLICATION_RECEIVED,
    SignalCategory.REJECTION: OutcomeKind.REJECTED,
    SignalCategory.INTERVIEW_INVITATION: OutcomeKind.INTERVIEW_REQUESTED,
    SignalCategory.RECRUITER_CONTACT: OutcomeKind.RECRUITER_CONTACT,
    SignalCategory.ASSESSMENT: OutcomeKind.ASSESSMENT_REQUESTED,
    SignalCategory.INFORMATION_REQUEST: OutcomeKind.UNDER_REVIEW,
    SignalCategory.WITHDRAWAL: OutcomeKind.WITHDRAWN,
    SignalCategory.DUPLICATE_OR_CLOSED: OutcomeKind.CLOSED_WITHOUT_APPLICATION,
    SignalCategory.STATUS_UPDATE: OutcomeKind.UNDER_REVIEW,
    SignalCategory.EXECUTION_RESULT: OutcomeKind.SUBMITTED,
    SignalCategory.OTHER: None,
    SignalCategory.UNKNOWN: None,
}

STAGE: dict[OutcomeKind, int] = {
    OutcomeKind.SUBMITTED: 1,
    OutcomeKind.APPLICATION_RECEIVED: 2,
    OutcomeKind.UNDER_REVIEW: 3,
    OutcomeKind.RECRUITER_CONTACT: 4,
    OutcomeKind.ASSESSMENT_REQUESTED: 4,
    OutcomeKind.INTERVIEW_REQUESTED: 5,
    OutcomeKind.INTERVIEW_SCHEDULED: 6,
}
TERMINAL: frozenset[OutcomeKind] = frozenset({OutcomeKind.REJECTED, OutcomeKind.WITHDRAWN, OutcomeKind.CLOSED_WITHOUT_APPLICATION})


def evidence_for(confidence: Confidence, source: ClassificationSource) -> EvidenceStrength:
    """Evidence strength of an outcome event derived from a classification."""
    if source is ClassificationSource.AI:
        return EvidenceStrength.WEAK
    if source in (ClassificationSource.HUMAN, ClassificationSource.DECLARED):
        return EvidenceStrength.STRONG
    if confidence is Confidence.HIGH:
        return EvidenceStrength.STRONG
    if confidence is Confidence.MEDIUM:
        return EvidenceStrength.MODERATE
    return EvidenceStrength.WEAK


def outcome_for_verification(status: str, run_outcome: Optional[str]) -> tuple[OutcomeKind, EvidenceStrength, Confidence]:
    """Execution evidence keeps the Phase 6/7/9 semantics exactly:
    VERIFIED → SUBMITTED (strong); LIKELY → SUBMITTED (moderate); UNKNOWN /
    PENDING / NOT_ATTEMPTED → UNKNOWN (weak; never upgraded); FAILED → NEEDS_REVIEW."""
    if status == VerificationStatus.VERIFIED.value:
        return OutcomeKind.SUBMITTED, EvidenceStrength.STRONG, Confidence.HIGH
    if status == VerificationStatus.LIKELY.value:
        return OutcomeKind.SUBMITTED, EvidenceStrength.MODERATE, Confidence.MEDIUM
    if status == VerificationStatus.FAILED.value:
        return OutcomeKind.NEEDS_REVIEW, EvidenceStrength.STRONG, Confidence.HIGH
    return OutcomeKind.UNKNOWN, EvidenceStrength.WEAK, Confidence.LOW


class _Event:
    """The subset of an outcome event the derivation reads (row or model)."""

    __slots__ = ("id", "outcome", "evidence", "event_at", "sequence", "retracted", "superseded_by_id")

    def __init__(self, source):
        self.id = source.id
        self.outcome = OutcomeKind(source.outcome)
        self.evidence = EvidenceStrength(source.evidence)
        self.event_at = source.event_at
        self.sequence = source.sequence or 0
        self.retracted = bool(getattr(source, "retracted", False))
        self.superseded_by_id = getattr(source, "superseded_by_id", None)


def derive(application_id: str, events: Iterable) -> DerivedOutcome:
    """Pure: the current status from the event history. See the module docstring."""
    active = sorted((_Event(e) for e in events if not getattr(e, "retracted", False) and getattr(e, "superseded_by_id", None) is None), key=lambda e: (e.event_at, e.sequence))
    result = DerivedOutcome(application_id=application_id, event_count=len(active), rules_version=OUTCOME_RULES_VERSION)
    if not active:
        return result
    result.last_event_at = active[-1].event_at
    conflicts: list[str] = []
    strong_terminal = [e for e in active if e.outcome in TERMINAL and e.evidence is EvidenceStrength.STRONG]
    weak_terminal = [e for e in active if e.outcome in TERMINAL and e.evidence is not EvidenceStrength.STRONG]
    progress = [e for e in active if e.outcome in STAGE and e.evidence in (EvidenceStrength.STRONG, EvidenceStrength.MODERATE)]
    weak_progress = [e for e in active if e.outcome in STAGE and e.evidence is EvidenceStrength.WEAK]
    review_flag = any(e.outcome is OutcomeKind.NEEDS_REVIEW for e in active)

    current: Optional[OutcomeKind] = None
    basis: Optional[_Event] = None
    if strong_terminal:
        last = strong_terminal[-1]
        kinds = {e.outcome for e in strong_terminal}
        if len(kinds) > 1:
            conflicts.append("multiple terminal outcomes: " + ", ".join(sorted(k.value for k in kinds)))
        later = [p for p in progress if (p.event_at, p.sequence) > (last.event_at, last.sequence)]
        if later:
            conflicts.append(f"{later[-1].outcome.value} evidence after {last.outcome.value}")
        current, basis = last.outcome, last
    elif progress:
        basis = max(progress, key=lambda e: (STAGE[e.outcome], e.event_at, e.sequence))
        current = basis.outcome

    provisional: Optional[OutcomeKind] = None
    if weak_terminal and current not in TERMINAL:
        # Shown, flagged for review, but a weak ending never downgrades an established status.
        provisional = weak_terminal[-1].outcome
    elif weak_progress:
        best = max(weak_progress, key=lambda e: (STAGE[e.outcome], e.event_at, e.sequence))
        if current is None or (current in STAGE and STAGE[best.outcome] > STAGE[current]):
            provisional = best.outcome

    if conflicts:
        result.current = OutcomeKind.NEEDS_REVIEW
    elif current is not None:
        result.current = current
    elif review_flag:
        result.current = OutcomeKind.NEEDS_REVIEW
    else:
        result.current = OutcomeKind.UNKNOWN
    result.provisional = provisional
    result.conflicts = conflicts
    result.needs_review = bool(conflicts) or review_flag or (provisional is not None and provisional != current)
    result.basis_event_id = basis.id if basis is not None else None
    return result


__all__ = ["OUTCOME_FOR_CATEGORY", "STAGE", "TERMINAL", "derive", "evidence_for", "outcome_for_verification"]
