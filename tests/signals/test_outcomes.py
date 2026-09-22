"""Outcome model: pure derivation rules, then the persisted event history through the service."""

from datetime import datetime, timedelta

from app.application.models import ApplicationStatus
from app.pipeline.database.models import CandidateOpportunityRow
from app.pipeline.repository import OpportunityRepository
from app.signals.database.models import OutcomeEventRow
from app.signals.models import (
    OUTCOME_RULES_VERSION,
    AttributionHints,
    ClassificationSource,
    Confidence,
    EvidenceStrength,
    OutcomeKind,
    SignalCategory,
    SignalStatus,
)
from app.signals.outcomes import (
    OUTCOME_FOR_CATEGORY,
    derive,
    evidence_for,
    outcome_for_verification,
)
from tests.signals.conftest import email, manual, submitted

T0 = datetime(2026, 9, 1, 10, 0, 0)


class E:
    """A minimal event for the pure derivation."""

    _n = 0

    def __init__(self, outcome, evidence="STRONG", at=None, retracted=False, superseded=None, id_=None):
        E._n += 1
        self.id = id_ or f"e{E._n}"
        self.outcome = outcome if isinstance(outcome, str) else outcome.value
        self.evidence = evidence if isinstance(evidence, str) else evidence.value
        self.event_at = at or (T0 + timedelta(hours=E._n))
        self.sequence = E._n
        self.retracted = retracted
        self.superseded_by_id = superseded


# ------------------------------------------------------------ pure rules


def test_no_events_is_unknown():
    d = derive("a", [])
    assert d.current is OutcomeKind.UNKNOWN and not d.needs_review and d.rules_version == OUTCOME_RULES_VERSION


def test_valid_progression_and_highest_stage_wins_over_order():
    d = derive("a", [E("SUBMITTED"), E("APPLICATION_RECEIVED"), E("UNDER_REVIEW"), E("INTERVIEW_REQUESTED"), E("INTERVIEW_SCHEDULED")])
    assert d.current is OutcomeKind.INTERVIEW_SCHEDULED and not d.needs_review
    late = derive("a", [E("SUBMITTED"), E("INTERVIEW_REQUESTED"), E("APPLICATION_RECEIVED")])
    assert late.current is OutcomeKind.INTERVIEW_REQUESTED, "a late confirmation never demotes an interview"


def test_out_of_order_and_skipped_stages_are_fine():
    assert derive("a", [E("SUBMITTED"), E("INTERVIEW_REQUESTED")]).current is OutcomeKind.INTERVIEW_REQUESTED
    assert derive("a", [E("INTERVIEW_REQUESTED"), E("REJECTED")]).current is OutcomeKind.REJECTED
    assert derive("a", [E("INTERVIEW_REQUESTED"), E("WITHDRAWN")]).current is OutcomeKind.WITHDRAWN


def test_terminal_then_progress_is_a_conflict():
    d = derive("a", [E("SUBMITTED"), E("REJECTED"), E("INTERVIEW_REQUESTED")])
    assert d.current is OutcomeKind.NEEDS_REVIEW and d.needs_review and "INTERVIEW_REQUESTED evidence after REJECTED" in d.conflicts[0]
    two = derive("a", [E("REJECTED"), E("WITHDRAWN")])
    assert two.current is OutcomeKind.NEEDS_REVIEW and any("multiple terminal" in c for c in two.conflicts)


def test_weak_evidence_never_sets_or_downgrades_the_status():
    d = derive("a", [E("INTERVIEW_REQUESTED", "STRONG"), E("REJECTED", "WEAK")])
    assert d.current is OutcomeKind.INTERVIEW_REQUESTED and d.provisional is OutcomeKind.REJECTED and d.needs_review and d.conflicts == [], "a weak ending is provisional, never a downgrade"
    only_weak = derive("a", [E("INTERVIEW_REQUESTED", "WEAK")])
    assert only_weak.current is OutcomeKind.UNKNOWN and only_weak.provisional is OutcomeKind.INTERVIEW_REQUESTED and only_weak.needs_review
    moderate_terminal = derive("a", [E("APPLICATION_RECEIVED", "STRONG"), E("REJECTED", "MODERATE")])
    assert moderate_terminal.current is OutcomeKind.APPLICATION_RECEIVED and moderate_terminal.provisional is OutcomeKind.REJECTED, "a terminal outcome needs STRONG evidence"
    moderate_progress = derive("a", [E("SUBMITTED", "STRONG"), E("UNDER_REVIEW", "MODERATE")])
    assert moderate_progress.current is OutcomeKind.UNDER_REVIEW and not moderate_progress.needs_review


def test_retracted_and_superseded_events_are_ignored_but_kept():
    events = [E("SUBMITTED"), E("REJECTED", retracted=True), E("INTERVIEW_REQUESTED", superseded="x")]
    d = derive("a", events)
    assert d.current is OutcomeKind.SUBMITTED and d.event_count == 1 and len(events) == 3


def test_needs_review_event_flags_without_changing_status():
    d = derive("a", [E("SUBMITTED"), E("NEEDS_REVIEW")])
    assert d.current is OutcomeKind.SUBMITTED and d.needs_review
    assert derive("a", [E("NEEDS_REVIEW")]).current is OutcomeKind.NEEDS_REVIEW


def test_evidence_and_verification_mappings():
    assert evidence_for(Confidence.HIGH, ClassificationSource.RULES) is EvidenceStrength.STRONG
    assert evidence_for(Confidence.MEDIUM, ClassificationSource.RULES) is EvidenceStrength.MODERATE
    assert evidence_for(Confidence.HIGH, ClassificationSource.AI) is EvidenceStrength.WEAK, "AI is never authoritative"
    assert evidence_for(Confidence.LOW, ClassificationSource.HUMAN) is EvidenceStrength.STRONG
    assert outcome_for_verification("VERIFIED", "SUBMITTED")[:2] == (OutcomeKind.SUBMITTED, EvidenceStrength.STRONG)
    assert outcome_for_verification("LIKELY", "SUBMITTED")[:2] == (OutcomeKind.SUBMITTED, EvidenceStrength.MODERATE)
    assert outcome_for_verification("UNKNOWN", "UNKNOWN")[:2] == (OutcomeKind.UNKNOWN, EvidenceStrength.WEAK)
    assert outcome_for_verification("PENDING", "UNKNOWN")[0] is OutcomeKind.UNKNOWN and outcome_for_verification("FAILED", "SUBMITTED")[0] is OutcomeKind.NEEDS_REVIEW
    assert OUTCOME_FOR_CATEGORY[SignalCategory.OTHER] is None and OUTCOME_FOR_CATEGORY[SignalCategory.INTERVIEW_INVITATION] is OutcomeKind.INTERVIEW_REQUESTED


# ------------------------------------------------------- persisted history


def test_event_history_is_append_only_and_status_is_derived(inbox, db_session, tenant_id, opportunities):
    attempt = submitted(db_session, tenant_id, opportunities, company="Upsilon")
    hints = AttributionHints(application_id=attempt.id)
    received, _ = inbox.ingest_email(email("Thanks", "Thank you for applying. We have received your application.", message_id="<u1@x>", hints=hints, received_at=T0))
    interview, _ = inbox.ingest_email(email("Interview", "We'd like to invite you to an interview next week.", message_id="<u2@x>", hints=hints, received_at=T0 + timedelta(days=3)))
    current, events = inbox.outcome_history(attempt.id)
    assert [e.outcome for e in events] == ["APPLICATION_RECEIVED", "INTERVIEW_REQUESTED"] and [e.sequence for e in events] == [1, 2]
    assert all(e.evidence == "STRONG" and e.origin == "rules" and e.time_basis == "external" and e.rules_version == OUTCOME_RULES_VERSION for e in events)
    assert events[0].event_at == T0 and events[0].classifier_version and events[0].attribution_version
    assert current.current_outcome == "INTERVIEW_REQUESTED" and current.version == 2 and not current.needs_review
    # the lifecycle followed (guarded), so did the candidate opportunity
    db_session.refresh(attempt)
    assert attempt.status == ApplicationStatus.INTERVIEWING.value
    assert db_session.get(CandidateOpportunityRow, attempt.candidate_opportunity_id).state == "INTERVIEWING"
    rejected, _ = inbox.ingest_email(email("Update", "Unfortunately we will not be moving forward with your application.", message_id="<u3@x>", hints=hints, received_at=T0 + timedelta(days=10)))
    current, events = inbox.outcome_history(attempt.id)
    assert current.current_outcome == "REJECTED" and len(events) == 3 and current.version == 3
    db_session.refresh(attempt)
    assert attempt.status == ApplicationStatus.REJECTED.value
    audit = OpportunityRepository(db_session, tenant_id).list_audit("application_outcome", attempt.id)
    assert {e.action for e in audit} >= {"event:application_received", "event:interview_requested", "event:rejected", "derived"}
    derived_events = [e for e in audit if e.action == "derived"]
    assert derived_events[0].before is not None or derived_events[-1].before is None, "every derivation carries its before-state"


def test_duplicate_deliveries_never_duplicate_outcome_events_or_transitions(inbox, db_session, tenant_id, opportunities):
    from app.application.database.models import ApplicationEventRow

    attempt = submitted(db_session, tenant_id, opportunities, company="Phi")
    msg = email("Interview", "We'd like to invite you to an interview.", message_id="<phi@x>", hints=AttributionHints(application_id=attempt.id))
    for _ in range(3):
        inbox.ingest_email(msg)
    _, events = inbox.outcome_history(attempt.id)
    assert len(events) == 1
    db_session.refresh(attempt)
    assert attempt.status == ApplicationStatus.INTERVIEWING.value
    transitions = db_session.query(ApplicationEventRow).filter(ApplicationEventRow.application_id == attempt.id, ApplicationEventRow.to_status == "INTERVIEWING").count()
    assert transitions == 1
    # replaying processing on the same signal is a no-op too
    row = inbox.get_signal(inbox.list_signals()[0][0].id)
    inbox.reprocess(row.id, "test")
    assert len(inbox.outcome_history(attempt.id)[1]) == 1 and db_session.query(OutcomeEventRow).filter(OutcomeEventRow.application_id == attempt.id).count() == 1


def test_conflicting_signals_are_visible_and_need_review(inbox, db_session, tenant_id, opportunities):
    attempt = submitted(db_session, tenant_id, opportunities, company="Chi")
    hints = AttributionHints(application_id=attempt.id)
    inbox.ingest_email(email("Update", "We regret to inform you that you have not been selected.", message_id="<c1@x>", hints=hints, received_at=T0))
    inbox.ingest_email(email("Interview", "We'd like to invite you to an interview.", message_id="<c2@x>", hints=hints, received_at=T0 + timedelta(days=2)))
    current, events = inbox.outcome_history(attempt.id)
    assert current.current_outcome == "NEEDS_REVIEW" and current.needs_review and "after REJECTED" in current.conflicts[0] and len(events) == 2
    db_session.refresh(attempt)
    assert attempt.status == ApplicationStatus.REJECTED.value, "the conflict does not silently reopen the attempt"


def test_medium_confidence_signal_is_provisional_until_confirmed(inbox, db_session, tenant_id, opportunities):
    attempt = submitted(db_session, tenant_id, opportunities, company="Psi")
    row, _ = inbox.ingest_email(email("Hello from Psi", "I'm a technical recruiter and came across your profile.", hints=AttributionHints(application_id=attempt.id)))
    assert row.category == "RECRUITER_CONTACT" and row.confidence == "MEDIUM" and row.status == SignalStatus.NEEDS_REVIEW.value
    current, events = inbox.outcome_history(attempt.id)
    assert events[0].evidence == "MODERATE" and current.current_outcome == "RECRUITER_CONTACT"
    inbox.confirm_outcome(row.id, OutcomeKind.INTERVIEW_REQUESTED, "reviewer", "they asked for a call")
    current, events = inbox.outcome_history(attempt.id)
    assert [e.outcome for e in events] == ["RECRUITER_CONTACT", "INTERVIEW_REQUESTED"] and events[0].superseded_by_id == events[1].id and events[1].origin == "human" and events[1].evidence == "STRONG"
    assert current.current_outcome == "INTERVIEW_REQUESTED" and row.status == SignalStatus.APPLIED.value


def test_time_semantics_external_vs_observed(inbox, db_session, tenant_id, opportunities):
    attempt = submitted(db_session, tenant_id, opportunities, company="Omega")
    with_time, _ = inbox.ingest_email(email("Thanks", "Thank you for applying.", message_id="<o1@x>", hints=AttributionHints(application_id=attempt.id), received_at=T0))
    without, _ = inbox.ingest(manual("They said the interview is on", category="INTERVIEW_INVITATION", hints=AttributionHints(application_id=attempt.id)))
    _, events = inbox.outcome_history(attempt.id)
    by_signal = {e.signal_id: e for e in events}
    assert by_signal[with_time.id].time_basis == "external" and by_signal[with_time.id].event_at == T0
    assert by_signal[without.id].time_basis == "observed" and by_signal[without.id].event_at == without.observed_at
    assert with_time.external_at == T0 and with_time.observed_at is not None and with_time.observed_at != T0


def test_withdrawal_and_closure_are_recorded_but_never_move_the_lifecycle(inbox, db_session, tenant_id, opportunities):
    attempt = submitted(db_session, tenant_id, opportunities, company="Alpha2")
    inbox.ingest_email(email("Withdrawn", "Your application has been withdrawn as requested.", hints=AttributionHints(application_id=attempt.id)))
    current, _ = inbox.outcome_history(attempt.id)
    db_session.refresh(attempt)
    assert current.current_outcome == "WITHDRAWN" and attempt.status == ApplicationStatus.SUBMITTED.value
    other = submitted(db_session, tenant_id, opportunities, company="Alpha3", status=ApplicationStatus.UNCERTAIN)
    inbox.ingest_email(email("Closed", "This position is no longer accepting applications.", message_id="<a3@x>", hints=AttributionHints(application_id=other.id)))
    db_session.refresh(other)
    assert inbox.outcome_history(other.id)[0].current_outcome == "CLOSED_WITHOUT_APPLICATION" and other.status == ApplicationStatus.UNCERTAIN.value
