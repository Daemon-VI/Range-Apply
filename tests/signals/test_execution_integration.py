"""Phase 6/7/9 execution results feed the inbox without a second verification
mechanism: VERIFIED / LIKELY / UNKNOWN / FAILED keep their meaning, a
redirect stays weaker than a reference, nothing is upgraded, nothing is duplicated."""

from app.application.models import ApplicationStatus
from app.execution.models import (
    ExecutionOutcome,
    ExecutionResult,
    ExecutorKind,
    VerificationMethod,
    VerificationResult,
    VerificationStatus,
)
from app.signals.database.models import SignalRow
from app.signals.execution import execution_signal
from app.signals.models import AttributionHints, OutcomeKind, SignalSource, SignalStatus
from app.signals.service import SignalInboxService
from tests.execution.conftest import (  # noqa: F401 - these tests drive mock submissions (LIVE)
    _live_submission_enabled,
    scripted,
)
from tests.signals.conftest import email


def _signals(db, tenant_id, application_id):
    return db.query(SignalRow).filter(SignalRow.tenant_id == tenant_id, SignalRow.application_id == application_id).order_by(SignalRow.created_at.asc()).all()


def test_verified_mock_result_becomes_a_strong_submitted_outcome(harness, db_session):
    attempt = harness.ready(company="Sig Verified")
    outcome = harness.execute(attempt)
    assert outcome["outcome"] == "SUBMITTED" and attempt.status == ApplicationStatus.VERIFIED.value
    signals = _signals(db_session, harness.tenant_id, attempt.id)
    assert len(signals) == 1
    s = signals[0]
    assert s.source == SignalSource.EXECUTION.value and s.category == "EXECUTION_RESULT" and s.classification_source == "execution" and s.confidence == "HIGH"
    assert s.status == SignalStatus.APPLIED.value and s.attribution_status == "MATCHED" and s.execution_run_id == attempt.last_execution_id
    assert s.payload["verification_status"] == "VERIFIED" and s.payload["kind"] == "execution_result"
    inbox = SignalInboxService(db_session, harness.tenant_id)
    current, events = inbox.outcome_history(attempt.id)
    assert current.current_outcome == "SUBMITTED" and events[0].evidence == "STRONG" and events[0].origin == "execution"
    trace = inbox.trace(s.id)
    assert trace.attempt["id"] == attempt.id and trace.preparation["id"] == attempt.preparation_id and trace.execution_runs[0]["verification_status"] == "VERIFIED" and trace.candidate_opportunity["id"] == attempt.candidate_opportunity_id


def test_unknown_result_stays_weak_then_confirmation_upgrades_through_verify(db_session, tenant_id, scheduler, opportunities, answered_bank):
    h = scripted(db_session, tenant_id, scheduler, opportunities, default="UNKNOWN")
    attempt = h.ready(company="Sig Unknown")
    assert h.execute(attempt)["outcome"] == "UNKNOWN" and attempt.status == ApplicationStatus.UNCERTAIN.value
    signals = _signals(db_session, tenant_id, attempt.id)
    assert len(signals) == 1 and signals[0].confidence == "LOW" and signals[0].status == SignalStatus.NEEDS_REVIEW.value
    inbox = SignalInboxService(db_session, tenant_id)
    current, events = inbox.outcome_history(attempt.id)
    assert current.current_outcome == "UNKNOWN" and events[0].evidence == "WEAK", "UNKNOWN is never upgraded by itself"
    # the person confirms the earlier submit (Phase 6 confirm) -> a second, stronger signal
    h.service.confirm(attempt.last_execution_id, True, reference="UNK-9")
    signals = _signals(db_session, tenant_id, attempt.id)
    assert len(signals) == 2 and signals[1].payload["verification_status"] == "VERIFIED" and signals[1].payload["verification_method"] == "user_confirmation"
    current, events = inbox.outcome_history(attempt.id)
    assert current.current_outcome == "SUBMITTED" and [e.evidence for e in events] == ["WEAK", "STRONG"]


def test_likely_redirect_is_moderate_and_a_reference_is_strong(harness, db_session):
    from app.execution.executors.extension import BrowserExtensionExecutor

    executor = BrowserExtensionExecutor()
    package = harness.service.package(harness.ready(company="Sig Likely"))
    likely = executor.verify(package, ExecutionResult(outcome=ExecutionOutcome.SUBMITTED, submit_attempted=True, application_url="https://jobs.example.com/x/apply/confirmation"))
    strong = executor.verify(package, ExecutionResult(outcome=ExecutionOutcome.SUBMITTED, submit_attempted=True, confirmation_reference="REF-1"))
    assert likely.status is VerificationStatus.LIKELY and strong.status is VerificationStatus.VERIFIED
    from app.signals.outcomes import outcome_for_verification

    assert outcome_for_verification(likely.status.value, "SUBMITTED")[1].value == "MODERATE" and outcome_for_verification(strong.status.value, "SUBMITTED")[1].value == "STRONG"


def test_extension_and_playwright_results_carry_their_source(harness, db_session):
    attempt = harness.ready(company="Sig Ext")
    from app.execution.database.models import ExecutionRunRow

    run = ExecutionRunRow(tenant_id=harness.tenant_id, application_id=attempt.id, executor_kind=ExecutorKind.BROWSER_EXTENSION.value, executor_version="extension-v1", idempotency_key=f"x:{attempt.id}", run_number=7, status="SUBMITTED", outcome="SUBMITTED", verification_status="LIKELY", verification_method="redirect_url", application_url="https://jobs.example.com/x/thanks")
    request = execution_signal(run, attempt)
    assert request.source is SignalSource.EXTENSION and request.source_reference == f"execution_run:{run.id}:LIKELY:redirect_url" and request.hints.execution_run_id == run.id
    run.executor_kind = ExecutorKind.PLAYWRIGHT_LOCAL.value
    assert execution_signal(run, attempt).source is SignalSource.PLAYWRIGHT
    run.executor_kind = ExecutorKind.MOCK.value
    assert execution_signal(run, attempt).source is SignalSource.EXECUTION
    assert "cookie" not in str(request.payload).lower() and "token" not in str(request.payload).lower()


def test_duplicate_execution_reports_do_not_duplicate_signals(harness, db_session):
    attempt = harness.ready(company="Sig Dup")
    harness.execute(attempt)
    run = harness.service.require_run(attempt.last_execution_id)
    # re-verifying with the same evidence is an observation, not a new signal
    harness.service.verify(run.id, VerificationResult(status=VerificationStatus.VERIFIED, method=VerificationMethod(run.verification_method), detail="again"))
    signals = _signals(db_session, harness.tenant_id, attempt.id)
    assert len(signals) == 1 and signals[0].observation_count == 2
    inbox = SignalInboxService(db_session, harness.tenant_id)
    assert len(inbox.outcome_history(attempt.id)[1]) == 1


def test_verification_failed_needs_review(db_session, tenant_id, scheduler, opportunities, answered_bank):
    h = scripted(db_session, tenant_id, scheduler, opportunities, default="UNKNOWN")
    attempt = h.ready(company="Sig Failed")
    h.execute(attempt)
    h.service.confirm(attempt.last_execution_id, False, note="never submitted")
    signals = _signals(db_session, tenant_id, attempt.id)
    failed = [s for s in signals if s.payload["verification_status"] == "FAILED"]
    assert failed and failed[0].status == SignalStatus.NEEDS_REVIEW.value
    current, events = inbox_history(db_session, tenant_id, attempt.id)
    assert current.needs_review and any(e.outcome == OutcomeKind.NEEDS_REVIEW.value for e in events)


def inbox_history(db, tenant_id, application_id):
    return SignalInboxService(db, tenant_id).outcome_history(application_id)


def test_confirmation_email_verifies_an_uncertain_attempt_through_the_phase6_path(db_session, tenant_id, scheduler, opportunities, answered_bank):
    h = scripted(db_session, tenant_id, scheduler, opportunities, default="UNKNOWN")
    attempt = h.ready(company="Sig Email Verify")
    h.execute(attempt)
    assert attempt.status == ApplicationStatus.UNCERTAIN.value
    inbox = SignalInboxService(db_session, tenant_id, actor="test")
    row, _ = inbox.ingest_email(email("Application received", "Thank you for applying. We have received your application. Application ID: EMV-42", hints=AttributionHints(application_id=attempt.id)))
    db_session.refresh(attempt)
    assert row.status == SignalStatus.APPLIED.value and attempt.status == ApplicationStatus.VERIFIED.value and attempt.confirmation == "EMV-42"
    run = h.service.require_run(attempt.last_execution_id)
    assert run.status == "VERIFIED" and run.verification_method == VerificationMethod.CONFIRMATION_EMAIL.value
    signals = _signals(db_session, tenant_id, attempt.id)
    assert [s.source for s in signals] == ["EXECUTION", "EMAIL", "EXECUTION"], "the verification itself is recorded as a signal"
    current, events = inbox.outcome_history(attempt.id)
    assert current.current_outcome == "APPLICATION_RECEIVED" and not current.needs_review
    # a weak (AI / medium) confirmation would not verify: only STRONG evidence does
    other = h.ready(company="Sig Email Weak")
    h.execute(other)
    weak, _ = inbox.ingest_email(email("Note", "We will review your application and be in touch if there is a fit.", message_id="<w@x>", hints=AttributionHints(application_id=other.id)))
    db_session.refresh(other)
    assert weak.confidence == "MEDIUM" and other.status == ApplicationStatus.UNCERTAIN.value


def test_signal_failure_never_breaks_execution(harness, db_session, monkeypatch):
    import app.signals.execution as sig_exec

    def boom(*args, **kwargs):
        raise RuntimeError("inbox down")

    monkeypatch.setattr(sig_exec, "emit_execution_signal", boom)
    attempt = harness.ready(company="Sig Boom")
    outcome = harness.execute(attempt)
    assert outcome["outcome"] == "SUBMITTED" and attempt.status == ApplicationStatus.VERIFIED.value
    assert _signals(db_session, harness.tenant_id, attempt.id) == []
