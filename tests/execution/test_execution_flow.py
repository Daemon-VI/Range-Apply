"""State transitions through the mock executor: success, retries, failures,
handoffs, unknown results, verification, user input, cancellation."""

from datetime import timedelta

import pytest

from app.application.models import ApplicationStatus
from app.core.errors import ConflictError
from app.core.timeutils import db_now
from app.execution.executors import mock as m
from app.execution.models import ExecutionStatus, ExecutorKind, HandoffReason, VerificationStatus
from app.pipeline.models import OpportunityState, QueueAction, QueueState
from tests.execution.conftest import scripted


def test_success_submits_verifies_and_consumes_reservation(harness, db_session):
    attempt = harness.ready()
    used_before = harness.scheduler.capacity().day.used
    outcome = harness.execute(attempt)
    assert outcome["outcome"] == "SUBMITTED" and outcome["attempt_status"] == ApplicationStatus.VERIFIED.value
    assert harness.mock.submit_calls[attempt.id] == 1 and harness.mock.verify_calls[attempt.id] == 1
    assert attempt.submitted_at and attempt.verified_at and attempt.external_application_id
    assert attempt.submission_key and attempt.execution_count == 1
    item = harness.item(attempt)
    assert item.state == QueueState.SUCCEEDED.value and item.result["outcome"] == "SUBMITTED"
    runs = harness.service.runs_for(attempt.id)
    assert len(runs) == 1 and runs[0].status == ExecutionStatus.VERIFIED.value and runs[0].submit_invoked
    assert runs[0].verification_status == VerificationStatus.VERIFIED.value and runs[0].confirmation_reference
    assert runs[0].idempotency_key.endswith(":1:1")
    co = harness.service.repo.get_candidate_opportunity(attempt.candidate_opportunity_id)
    assert co.state == OpportunityState.VERIFIED.value
    # The admission reservation was consumed, not counted again.
    assert harness.scheduler.capacity().day.used == used_before
    # Scheduler now sees it as completed; execution never enqueues a second SUBMIT.
    run = harness.scheduler.run()
    assert run.already_completed == 1 and run.execution_enqueued == 0
    assert harness.service.summary().metrics["submitted"] == 1


def test_likely_submission_stays_submitted(db_session, tenant_id, scheduler, opportunities, answered_bank):
    h = scripted(db_session, tenant_id, scheduler, opportunities, default=m.SUCCESS_LIKELY)
    attempt = h.ready()
    h.execute(attempt)
    assert attempt.status == ApplicationStatus.SUBMITTED.value and attempt.verified_at is None
    run = h.service.runs_for(attempt.id)[0]
    assert run.status == ExecutionStatus.SUBMITTED.value and run.verification_status == VerificationStatus.LIKELY.value


def test_retryable_failure_keeps_attempt_ready_and_slot(db_session, tenant_id, scheduler, opportunities, answered_bank):
    h = scripted(db_session, tenant_id, scheduler, opportunities, default=m.RETRYABLE)
    attempt = h.ready()
    outcome = h.execute(attempt)
    assert outcome["outcome"] == "RETRYABLE_FAILURE"
    assert attempt.status == ApplicationStatus.READY.value and attempt.submission_key is None and attempt.released_at is None
    item = h.item(attempt)
    assert item.state == QueueState.RETRY_WAIT.value and item.attempts == 1
    assert h.mock.submit_calls[attempt.id] == 0
    # The next try (after the backoff) succeeds; exactly one submit overall.
    h.mock.default = m.SUCCESS
    item.available_at = db_now() - timedelta(seconds=1)
    db_session.commit()
    h.execute(attempt, worker="w2")
    assert attempt.status == ApplicationStatus.VERIFIED.value and h.mock.submit_calls[attempt.id] == 1
    runs = h.service.runs_for(attempt.id)
    assert [r.run_number for r in runs] == [2, 1] and runs[1].status == ExecutionStatus.FAILED_RETRYABLE.value


def test_retries_exhausted_needs_review(db_session, tenant_id, scheduler, opportunities, answered_bank):
    h = scripted(db_session, tenant_id, scheduler, opportunities, default=m.RETRYABLE)
    attempt = h.ready()
    for i in range(3):
        item = h.item(attempt)
        item.available_at = db_now() - timedelta(seconds=1)
        db_session.commit()
        h.execute(attempt, worker=f"w{i}")
    assert h.item(attempt).state == QueueState.FAILED.value
    assert attempt.status == ApplicationStatus.NEEDS_REVIEW.value and attempt.released_at is None
    assert h.mock.submit_calls[attempt.id] == 0


def test_permanent_failure_releases_slot(db_session, tenant_id, scheduler, opportunities, answered_bank):
    h = scripted(db_session, tenant_id, scheduler, opportunities, default=m.PERMANENT)
    attempt = h.ready()
    used = h.scheduler.capacity().day.used
    h.execute(attempt)
    assert attempt.status == ApplicationStatus.FAILED.value and attempt.released_at is not None
    assert h.item(attempt).state == QueueState.FAILED.value
    assert h.scheduler.capacity().day.used == used - 1
    assert h.service.summary().metrics["permanent_failures"] == 1


@pytest.mark.parametrize("script, reason", [(m.CAPTCHA, HandoffReason.CAPTCHA_REQUIRED), (m.AUTH, HandoffReason.AUTH_REQUIRED), (m.MFA, HandoffReason.MFA_REQUIRED)])
def test_handoff_pauses_without_bypass(db_session, tenant_id, scheduler, opportunities, answered_bank, script, reason):
    h = scripted(db_session, tenant_id, scheduler, opportunities, default=script)
    attempt = h.ready()
    outcome = h.execute(attempt)
    assert outcome["outcome"] == "HANDOFF"
    assert attempt.status == ApplicationStatus.BLOCKED.value and attempt.blocked_reason == reason.value
    assert attempt.released_at is None, "the slot is held while a person finishes"
    run = h.service.runs_for(attempt.id)[0]
    assert run.status == ExecutionStatus.HANDOFF.value and run.handoff_reason == reason.value
    assert run.handoff["stopped_at"] and run.handoff["remaining_steps"] and run.handoff["resumable"] is True
    assert h.item(attempt).state == QueueState.BLOCKED.value
    assert h.mock.submit_calls[attempt.id] == 0
    assert h.service.summary().handoffs_by_reason == {reason.value: 1}


def test_handoff_then_candidate_confirms_submission(db_session, tenant_id, scheduler, opportunities, answered_bank):
    h = scripted(db_session, tenant_id, scheduler, opportunities, default=m.CAPTCHA)
    attempt = h.ready()
    h.execute(attempt)
    run = h.service.runs_for(attempt.id)[0]
    h.service.confirm(run.id, submitted=True, reference="ACME-123")
    h.refresh(attempt)
    assert attempt.status == ApplicationStatus.VERIFIED.value and attempt.confirmation == "ACME-123"
    db_session.refresh(run)
    assert run.status == ExecutionStatus.VERIFIED.value and run.verification_method == "user_confirmation"
    assert h.item(attempt).state == QueueState.SUCCEEDED.value


def test_handoff_then_retry_resumes(db_session, tenant_id, scheduler, opportunities, answered_bank):
    h = scripted(db_session, tenant_id, scheduler, opportunities, default=m.AUTH)
    attempt = h.ready()
    h.execute(attempt)
    h.mock.default = m.SUCCESS
    h.service.retry(attempt.id, "user", "signed in")
    h.refresh(attempt)
    assert attempt.status == ApplicationStatus.READY.value and attempt.blocked_reason is None
    assert h.item(attempt).state == QueueState.PENDING.value
    h.execute(attempt, worker="w2")
    assert attempt.status == ApplicationStatus.VERIFIED.value and h.mock.submit_calls[attempt.id] == 1


def test_form_changed_needs_review(db_session, tenant_id, scheduler, opportunities, answered_bank):
    h = scripted(db_session, tenant_id, scheduler, opportunities, default=m.FORM_CHANGED)
    attempt = h.ready()
    h.execute(attempt)
    assert attempt.status == ApplicationStatus.NEEDS_REVIEW.value and attempt.status_reason.startswith("FORM_CHANGED")
    assert h.item(attempt).state == QueueState.NEEDS_REVIEW.value and h.mock.submit_calls[attempt.id] == 0


def test_unknown_result_is_never_resubmitted(db_session, tenant_id, scheduler, opportunities, answered_bank):
    h = scripted(db_session, tenant_id, scheduler, opportunities, default=m.UNKNOWN)
    attempt = h.ready()
    outcome = h.execute(attempt)
    assert outcome["outcome"] == "UNKNOWN"
    assert attempt.status == ApplicationStatus.UNCERTAIN.value
    run = h.service.runs_for(attempt.id)[0]
    assert run.status == ExecutionStatus.UNKNOWN.value and run.submit_invoked and run.verification_status == VerificationStatus.PENDING.value
    assert run.diagnostics.get("password") == "[redacted]"
    assert h.item(attempt).state == QueueState.NEEDS_REVIEW.value
    with pytest.raises(ConflictError):
        h.service.retry(attempt.id, "user")
    with pytest.raises(ConflictError):
        h.service.cancel(attempt.id, "user")
    # Even if the item is forced back onto the queue, start() refuses to execute.
    item = h.item(attempt)
    h.service.queue.requeue(item, "user", "oops")
    db_session.commit()
    assert h.service.run_queue("w9", limit=5, executor_kind=ExecutorKind.MOCK)["awaiting_verification"] == 1
    assert h.mock.submit_calls[attempt.id] == 1
    # Verification settles it.
    h.mock.default = m.UNKNOWN_THEN_VERIFIED
    h.service.verify(run.id, executor_kind=ExecutorKind.MOCK)
    h.refresh(attempt)
    assert attempt.status == ApplicationStatus.VERIFIED.value
    assert h.item(attempt).state == QueueState.SUCCEEDED.value
    assert h.service.summary().metrics["unknown_results"] == 1


def test_unknown_then_verification_failed_needs_a_person(db_session, tenant_id, scheduler, opportunities, answered_bank):
    h = scripted(db_session, tenant_id, scheduler, opportunities, default=m.UNKNOWN_THEN_FAILED)
    attempt = h.ready()
    h.execute(attempt)
    run = h.service.runs_for(attempt.id)[0]
    h.service.verify(run.id, executor_kind=ExecutorKind.MOCK)
    h.refresh(attempt)
    assert attempt.status == ApplicationStatus.NEEDS_REVIEW.value
    db_session.refresh(run)
    assert run.status == ExecutionStatus.VERIFICATION_FAILED.value
    # A person may now retry; it is a deliberate second submit, counted as such.
    h.mock.default = m.SUCCESS
    h.service.retry(attempt.id, "user", "history shows nothing; apply again")
    h.execute(attempt, worker="w2")
    assert attempt.status == ApplicationStatus.VERIFIED.value and h.mock.submit_calls[attempt.id] == 2


def test_missing_fact_blocks_until_the_candidate_answers(db_session, tenant_id, scheduler, opportunities, answered_bank):
    h = scripted(db_session, tenant_id, scheduler, opportunities, default=m.NEEDS_INPUT)
    attempt = h.ready()
    # The bank's salary answer is prose; the form wants a number: the candidate decides.
    outcome = h.execute(attempt)
    assert outcome["outcome"] == "NEEDS_USER_INPUT"
    assert attempt.status == ApplicationStatus.NEEDS_USER_INPUT.value and h.mock.submit_calls[attempt.id] == 0
    preview = h.service.preview(attempt.id)
    field = next(f for f in preview.field_answers if f.status.value == "NEEDS_USER_INPUT")
    assert field.field_type.value == "numeric" and field.required
    h.service.answer_field(field.field_id, "1200000", "user")
    h.refresh(attempt)
    assert attempt.status == ApplicationStatus.READY.value, "answering the last blocker resumes the attempt"
    h.execute(attempt, worker="w2")
    assert attempt.status == ApplicationStatus.VERIFIED.value
    answered = next(f for f in h.service.preview(attempt.id).field_answers if f.field_id == field.field_id)
    assert answered.status.value == "ANSWERED" and answered.source.value == "USER" and answered.answer == "1200000"


def test_missing_contact_fact_blocks_instead_of_being_invented(db_session, tenant_id, scheduler, opportunities, answered_bank):
    h = scripted(db_session, tenant_id, scheduler, opportunities, contact=False)  # seed profile has no email
    attempt = h.ready()
    outcome = h.execute(attempt)
    assert outcome["outcome"] == "NEEDS_USER_INPUT" and h.mock.submit_calls[attempt.id] == 0
    field = next(f for f in h.service.preview(attempt.id).field_answers if f.status.value == "NEEDS_USER_INPUT")
    assert field.label == "Email" and "profile has no email" in field.reason


def test_unknown_required_field_hands_off(db_session, tenant_id, scheduler, opportunities, answered_bank):
    h = scripted(db_session, tenant_id, scheduler, opportunities, default=m.UNKNOWN_FIELD)
    attempt = h.ready()
    h.execute(attempt)
    assert attempt.status == ApplicationStatus.BLOCKED.value and attempt.blocked_reason == HandoffReason.UNKNOWN_REQUIRED_FIELD.value
    assert h.mock.submit_calls[attempt.id] == 0


def test_crash_before_submit_is_retryable_and_after_submit_is_unknown(db_session, tenant_id, scheduler, opportunities, answered_bank):
    h = scripted(db_session, tenant_id, scheduler, opportunities, default=m.CRASH_BEFORE_SUBMIT)
    a = h.ready(company="Alpha")
    h.execute(a)
    assert a.status == ApplicationStatus.READY.value and h.item(a).state == QueueState.RETRY_WAIT.value
    assert h.service.runs_for(a.id)[0].submit_invoked is False and h.mock.submit_calls[a.id] == 0
    h.mock.default = m.CRASH_AFTER_SUBMIT
    b = h.ready(company="Beta")
    h.execute(b, worker="w2")
    assert b.status == ApplicationStatus.UNCERTAIN.value and h.mock.submit_calls[b.id] == 1
    assert h.service.runs_for(b.id)[0].submit_invoked is True


def test_cancel_releases_slot_and_item(harness, db_session):
    attempt = harness.ready()
    used = harness.scheduler.capacity().day.used
    harness.service.cancel(attempt.id, "user", "changed my mind")
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.CANCELLED.value and attempt.released_at is not None
    assert harness.item(attempt).state == QueueState.CANCELLED.value
    assert harness.scheduler.capacity().day.used == used - 1
    # A retry after cancellation is a new attempt number and takes a fresh slot at execution.
    harness.service.retry(attempt.id, "user", "on second thought")
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.READY.value and attempt.attempt_number == 2 and attempt.cap_day is None
    harness.execute(attempt)
    assert attempt.status == ApplicationStatus.VERIFIED.value and attempt.cap_day is not None
    assert harness.scheduler.capacity().day.used == used


def test_manual_executor_hands_off_to_the_candidate(harness):
    attempt = harness.ready()
    outcome = harness.execute(attempt, executor=ExecutorKind.MANUAL)
    assert outcome["outcome"] == "HANDOFF"
    assert attempt.status == ApplicationStatus.BLOCKED.value and attempt.blocked_reason == HandoffReason.USER_CONFIRMATION_REQUIRED.value
    assert harness.mock.submit_calls[attempt.id] == 0


def test_run_queue_counts_and_prepare_item_untouched(db_session, tenant_id, scheduler, opportunities, answered_bank):
    h = scripted(db_session, tenant_id, scheduler, opportunities, script={}, default=m.SUCCESS)
    a = h.ready(company="Alpha")
    b = h.ready(company="Beta")
    h.mock.script[b.id] = m.CAPTCHA
    counts = h.service.run_queue("worker", limit=10, executor_kind=ExecutorKind.MOCK)
    assert counts["claimed"] == 2 and counts["submitted"] == 1 and counts["handoff"] == 1
    assert h.service.queue.find(a.opportunity_id, QueueAction.PREPARE).state == QueueState.SUCCEEDED.value
    assert h.service.run_queue("worker", limit=10, executor_kind=ExecutorKind.MOCK)["claimed"] == 0


def test_recapturing_a_known_form_keeps_same_labelled_controls_apart(harness):
    """Greenhouse labels both uploads "Attach" (Phase 13). The second capture of
    the same form must not hand the resume control the cover-letter answer."""
    from app.execution.models import ExecutorKind, FieldType, FormField, FormSnapshot

    attempt = harness.ready(company="Attach Twice")
    fields = [FormField(external_id="resume", label="Attach", field_type=FieldType.FILE, required=False), FormField(external_id="cover_letter", label="Attach", field_type=FieldType.FILE, required=False), FormField(external_id="email", label="Email", field_type=FieldType.EMAIL, required=True)]
    form = FormSnapshot(source_url="https://example.test/apply", fields=fields, executor_kind=ExecutorKind.MOCK, executor_version="test", metadata={})
    first, answers = harness.service.capture_form(attempt, form)
    second, again = harness.service.capture_form(attempt, form)
    assert second.id == first.id
    for batch in (answers, again):
        by_id = {a.external_id: a for a in batch}
        assert by_id["resume"].artifact_type == "RESUME"
        assert by_id["cover_letter"].artifact_type in (None, "COVER_LETTER")
        assert by_id["email"].answer == "ribhu@example.com"
