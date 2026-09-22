"""Increment 5: deriving desktop notifications from existing rows.

Nothing is stored: a notification is read out of ``application_events``,
``execution_runs`` and ``signals``, and a tiny local JSON cursor remembers
what was already said. These tests pin the vocabulary, the privacy contract
(a toast can appear over any other window) and the "never twice" rule.
"""

import json
import uuid
from datetime import timedelta

import pytest

from app.application.database.models import ApplicationEventRow
from app.application.models import ApplicationStatus
from app.config import settings
from app.core.timeutils import db_now
from app.desktop.notifications import model as notification_model
from app.desktop.notifications.cursor import (
    APPLICATION_EVENTS,
    FILE_NAME,
    Cursor,
    CursorStore,
)
from app.desktop.notifications.events import derive_events
from app.execution.database.models import ExecutionRunRow
from app.scheduler.attempts import AttemptRepository
from app.signals.database.models import SignalRow
from tests.scheduler.conftest import (  # noqa: F401
    answered_bank,
    db_session,
    evidence,
    fake_attempt,
    jobs,
    matches,
    opportunities,
    other_tenant_id,
    scheduler,
    set_policy,
    tenant_id,
)

COMPANY = "Notifiable"
ROLE = "Platform Engineer"


# ------------------------------------------------------------------ helpers


@pytest.fixture
def base():
    """A watermark comfortably before anything a test writes."""
    return db_now() - timedelta(hours=1)


@pytest.fixture
def cursor(base):
    fresh = Cursor()
    fresh.start(base)
    return fresh


@pytest.fixture
def attempt(db_session, tenant_id, opportunities):  # noqa: F811
    co = opportunities.make(title=ROLE, company=COMPANY)
    return fake_attempt(db_session, tenant_id, co, status=ApplicationStatus.READY, submitted_days_ago=None)


def _company_name(db_session, attempt):  # noqa: F811
    from app.pipeline.database.models import OpportunityRow

    return db_session.get(OpportunityRow, attempt.opportunity_id).company


def _event(db_session, attempt, to_status, at, from_status="PREPARING", metadata=None):  # noqa: F811
    row = ApplicationEventRow(
        id=uuid.uuid4().hex,
        application_id=attempt.id,
        event_type=f"attempt:{to_status.lower()}",
        from_status=from_status,
        to_status=to_status,
        metadata_=metadata or {},
        created_at=at,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _run(db_session, attempt, at, **fields):  # noqa: F811
    row = ExecutionRunRow(
        tenant_id=attempt.tenant_id,
        application_id=attempt.id,
        executor_kind="PLAYWRIGHT_LOCAL",
        executor_version="test",
        idempotency_key=uuid.uuid4().hex,
        run_number=1,
        status="FAILED_RETRYABLE",
        outcome="RETRYABLE_FAILURE",
        error_class="TIMEOUT",
        started_at=at - timedelta(seconds=10),
        finished_at=at,
    )
    for key, value in fields.items():
        setattr(row, key, value)
    db_session.add(row)
    db_session.commit()
    return row


def _signal(db_session, tenant_id, category, at, **fields):  # noqa: F811
    row = SignalRow(
        tenant_id=tenant_id,
        source="EMAIL",
        content_hash=uuid.uuid4().hex,
        dedupe_key=uuid.uuid4().hex,
        category=category,
        status="NEW",
        created_at=at,
        observed_at=at,
        last_observed_at=at,
    )
    for key, value in fields.items():
        setattr(row, key, value)
    db_session.add(row)
    db_session.commit()
    return row


def _status_set(db_session, attempt, status: ApplicationStatus, blocked_reason=None):  # noqa: F811
    attempt.status = status.value
    if blocked_reason is not None:
        attempt.blocked_reason = blocked_reason
    db_session.commit()


# ---------------------------------------------------- attempt transitions


def test_a_new_ready_attempt_is_announced_once_with_the_review_path(db_session, tenant_id, attempt, cursor, base):  # noqa: F811
    _event(db_session, attempt, "READY", base + timedelta(minutes=1))

    events, moved = derive_events(db_session, tenant_id, cursor)

    assert len(events) == 1
    note = events[0]
    assert note.kind == notification_model.APPLICATION_READY
    assert note.title == "Application Ready"
    assert note.path == f"/desktop/applications/{attempt.id}"
    assert note.application_id == attempt.id and note.tenant_id == tenant_id
    assert ROLE in note.body and _company_name(db_session, attempt) in note.body
    assert "ready for review" in note.body

    again, _ = derive_events(db_session, tenant_id, moved)
    assert again == []


def test_the_real_attempt_repository_transition_is_what_derivation_reads(db_session, tenant_id, attempt, cursor):  # noqa: F811
    """Not a synthetic row: the event the execution path actually writes."""
    repo = AttemptRepository(db_session, tenant_id)
    repo.transition(attempt, ApplicationStatus.BLOCKED, actor="test", reason="CAPTCHA_REQUIRED: a challenge appeared on the page")
    attempt.blocked_reason = "CAPTCHA_REQUIRED"
    db_session.commit()

    events, _ = derive_events(db_session, tenant_id, cursor)

    assert [e.kind for e in events] == [notification_model.ATTENTION_REQUIRED]
    assert events[0].reason == "CAPTCHA_REQUIRED"
    assert "a challenge appeared" not in events[0].body


def test_a_ready_event_for_an_attempt_that_moved_on_is_not_announced(db_session, tenant_id, attempt, cursor, base):  # noqa: F811
    _event(db_session, attempt, "READY", base + timedelta(minutes=1))
    _status_set(db_session, attempt, ApplicationStatus.SUBMITTING)

    events, _ = derive_events(db_session, tenant_id, cursor)

    assert events == []


@pytest.mark.parametrize(
    "reason,phrase",
    [
        ("CAPTCHA_REQUIRED", "CAPTCHA requires your attention"),
        ("MFA_REQUIRED", "A verification code is required"),
        ("UNSUPPORTED_FORM", "The form cannot be filled automatically"),
    ],
)
def test_blocked_says_what_kind_of_attention_is_needed(db_session, tenant_id, attempt, cursor, base, reason, phrase):  # noqa: F811
    _status_set(db_session, attempt, ApplicationStatus.BLOCKED, blocked_reason=reason)
    _event(db_session, attempt, "BLOCKED", base + timedelta(minutes=1), from_status="SUBMITTING", metadata={"reason": reason, "message": "raw executor text"})

    events, _ = derive_events(db_session, tenant_id, cursor)

    assert len(events) == 1
    note = events[0]
    assert note.kind == notification_model.ATTENTION_REQUIRED
    assert note.reason == reason
    assert note.body == f"{phrase} for {_company_name(db_session, attempt)}."
    assert note.importance == "high"
    assert "raw executor text" not in note.body


def test_needs_user_input_asks_for_an_answer(db_session, tenant_id, attempt, cursor, base):  # noqa: F811
    _status_set(db_session, attempt, ApplicationStatus.NEEDS_USER_INPUT)
    _event(db_session, attempt, "NEEDS_USER_INPUT", base + timedelta(minutes=1), from_status="READY")

    events, _ = derive_events(db_session, tenant_id, cursor)

    assert len(events) == 1
    assert events[0].kind == notification_model.ATTENTION_REQUIRED
    assert events[0].reason == "NEEDS_USER_INPUT"
    assert events[0].body.startswith("An answer is needed for ")


def test_submitted_verified_and_uncertain_each_say_their_own_thing(db_session, tenant_id, opportunities, cursor, base):  # noqa: F811
    company = None
    bodies = {}
    for index, status in enumerate(("SUBMITTED", "VERIFIED", "UNCERTAIN")):
        co = opportunities.make(title=ROLE, company=f"{COMPANY}{index}")
        row = fake_attempt(db_session, tenant_id, co, status=getattr(ApplicationStatus, status), submitted_days_ago=None)
        _event(db_session, row, status, base + timedelta(minutes=1 + index), from_status="SUBMITTING")
        bodies[status] = row
        company = company or _company_name(db_session, row)

    events, _ = derive_events(db_session, tenant_id, cursor)
    by_kind = {}
    for note in events:
        by_kind.setdefault(note.kind, []).append(note)

    submitted = by_kind[notification_model.SUBMISSION_COMPLETED][0]
    assert "was submitted." in submitted.body and ROLE in submitted.body

    verifications = {n.reason: n for n in by_kind[notification_model.VERIFICATION_RESULT]}
    assert "was verified." in verifications["VERIFIED"].body
    uncertain = verifications["UNCERTAIN"]
    assert "could not be verified" in uncertain.body
    assert "Please confirm the outcome" in uncertain.body and "will not resubmit" in uncertain.body
    assert uncertain.importance == "high"


def test_a_failed_attempt_reports_an_execution_failure(db_session, tenant_id, attempt, cursor, base):  # noqa: F811
    _status_set(db_session, attempt, ApplicationStatus.FAILED)
    _event(db_session, attempt, "FAILED", base + timedelta(minutes=1), from_status="SUBMITTING")

    events, _ = derive_events(db_session, tenant_id, cursor)

    assert [e.kind for e in events] == [notification_model.EXECUTION_FAILED]
    assert events[0].body == f"Execution failed for {ROLE} at {_company_name(db_session, attempt)}."
    assert events[0].reason == "FAILED"


def test_an_unremarkable_transition_says_nothing(db_session, tenant_id, attempt, cursor, base):  # noqa: F811
    _event(db_session, attempt, "PREPARING", base + timedelta(minutes=1), from_status="QUALIFIED")
    _event(db_session, attempt, "SUBMITTING", base + timedelta(minutes=2), from_status="READY")

    events, moved = derive_events(db_session, tenant_id, cursor)

    assert events == []
    # The cursor still moved past them: they are never looked at again.
    assert moved.watermark(APPLICATION_EVENTS) == base + timedelta(minutes=2)


# ------------------------------------------------------------ failed runs


def test_a_failed_run_that_left_the_attempt_ready_is_announced_without_the_error(db_session, tenant_id, attempt, cursor, base):  # noqa: F811
    secret = "Sorry, the page said mrs. candidate@example.com is not allowed"
    _run(db_session, attempt, base + timedelta(minutes=1), error_message=secret)
    # The real service hands a retryable failure back with SUBMITTING -> READY:
    # that is not a second "ready for review", only the failure is announced.
    _event(db_session, attempt, "READY", base + timedelta(minutes=1), from_status="SUBMITTING")

    events, moved = derive_events(db_session, tenant_id, cursor)

    assert [e.kind for e in events] == [notification_model.EXECUTION_FAILED]
    note = events[0]
    assert note.reason == "TIMEOUT"
    assert note.path == f"/desktop/applications/{attempt.id}"
    assert secret not in note.body and "candidate@example.com" not in note.body
    assert derive_events(db_session, tenant_id, moved)[0] == []


def test_a_handoff_run_is_left_to_the_attempt_event(db_session, tenant_id, attempt, cursor, base):  # noqa: F811
    _run(db_session, attempt, base + timedelta(minutes=1), status="HANDOFF", outcome="HANDOFF", handoff_reason="CAPTCHA_REQUIRED")

    events, _ = derive_events(db_session, tenant_id, cursor)

    assert events == []


def test_a_failure_that_moved_the_attempt_is_not_reported_twice(db_session, tenant_id, attempt, cursor, base):  # noqa: F811
    _status_set(db_session, attempt, ApplicationStatus.FAILED)
    _run(db_session, attempt, base + timedelta(minutes=1), status="FAILED_PERMANENT", outcome="PERMANENT_FAILURE")
    _event(db_session, attempt, "FAILED", base + timedelta(minutes=1), from_status="SUBMITTING")

    events, _ = derive_events(db_session, tenant_id, cursor)

    assert [e.kind for e in events] == [notification_model.EXECUTION_FAILED]
    assert events[0].reason == "FAILED"  # the attempt event, not the run


# ---------------------------------------------------------------- signals


def test_an_attributed_interview_signal_names_the_company(db_session, tenant_id, attempt, cursor, base):  # noqa: F811
    _signal(db_session, tenant_id, "INTERVIEW_INVITATION", base + timedelta(minutes=1), application_id=attempt.id, opportunity_id=attempt.opportunity_id, subject="Interview with the hiring manager")

    events, _ = derive_events(db_session, tenant_id, cursor)

    assert [e.kind for e in events] == [notification_model.INTERVIEW_SIGNAL]
    note = events[0]
    assert note.body == f"CareerOS detected a possible interview signal for {_company_name(db_session, attempt)}."
    assert note.path == f"/desktop/applications/{attempt.id}"
    assert note.reason == "INTERVIEW_INVITATION"


def test_an_unmatched_signal_names_no_company_and_points_at_the_inbox(db_session, tenant_id, cursor, base):  # noqa: F811
    signal = _signal(db_session, tenant_id, "INTERVIEW_INVITATION", base + timedelta(minutes=1), status="UNMATCHED", attribution_status="UNMATCHED")

    events, _ = derive_events(db_session, tenant_id, cursor)

    assert len(events) == 1
    assert events[0].body == "CareerOS detected a possible interview signal."
    assert events[0].path == f"/dashboard/signals/{signal.id}"
    assert events[0].application_id is None


def test_a_rejection_signal_is_a_rejection_signal(db_session, tenant_id, attempt, cursor, base):  # noqa: F811
    _signal(db_session, tenant_id, "REJECTION", base + timedelta(minutes=1), application_id=attempt.id, opportunity_id=attempt.opportunity_id)

    events, _ = derive_events(db_session, tenant_id, cursor)

    assert [e.kind for e in events] == [notification_model.REJECTION_SIGNAL]
    assert "rejection signal for" in events[0].body


@pytest.mark.parametrize(
    "category,fragment",
    [
        ("RECRUITER_CONTACT", "A recruiter may have contacted you about "),
        ("ASSESSMENT", "An assessment may have been requested by "),
        ("INFORMATION_REQUEST", " may have asked for more information."),
    ],
)
def test_the_quieter_signal_categories_get_one_sentence_each(db_session, tenant_id, attempt, cursor, base, category, fragment):  # noqa: F811
    _signal(db_session, tenant_id, category, base + timedelta(minutes=1), application_id=attempt.id, opportunity_id=attempt.opportunity_id)

    events, _ = derive_events(db_session, tenant_id, cursor)

    assert [e.kind for e in events] == [notification_model.OTHER_SIGNAL]
    assert fragment in events[0].body and events[0].reason == category


def test_a_repeated_observation_of_the_same_message_says_nothing_new(db_session, tenant_id, attempt, cursor, base):  # noqa: F811
    signal = _signal(db_session, tenant_id, "REJECTION", base + timedelta(minutes=1), application_id=attempt.id, opportunity_id=attempt.opportunity_id)

    events, moved = derive_events(db_session, tenant_id, cursor)
    assert len(events) == 1

    # The inbox saw the same mail again: same row, bumped counters only.
    signal.observation_count = 2
    signal.last_observed_at = db_now()
    db_session.commit()

    assert derive_events(db_session, tenant_id, moved)[0] == []


def test_executions_own_evidence_is_never_announced_as_a_signal(db_session, tenant_id, attempt, cursor, base):  # noqa: F811
    _signal(db_session, tenant_id, "EXECUTION_RESULT", base + timedelta(minutes=1), source="EXECUTION", application_id=attempt.id)
    _signal(db_session, tenant_id, "REJECTION", base + timedelta(minutes=2), source="PLAYWRIGHT", application_id=attempt.id)

    events, _ = derive_events(db_session, tenant_id, cursor)

    assert events == []


# --------------------------------------------------- collapse and tenancy


def test_several_of_one_kind_for_one_application_collapse_to_the_latest(db_session, tenant_id, attempt, cursor, base):  # noqa: F811
    _status_set(db_session, attempt, ApplicationStatus.BLOCKED, blocked_reason="AUTH_REQUIRED")
    first = _event(db_session, attempt, "BLOCKED", base + timedelta(minutes=1), from_status="SUBMITTING")
    last = _event(db_session, attempt, "BLOCKED", base + timedelta(minutes=5), from_status="READY")

    events, _ = derive_events(db_session, tenant_id, cursor)

    assert len(events) == 1
    assert last.id in events[0].key and first.id not in events[0].key


def test_another_tenants_rows_are_never_seen(db_session, tenant_id, other_tenant_id, opportunities, cursor, base):  # noqa: F811
    co = opportunities.make(title=ROLE, company="Somebody Else")
    theirs = fake_attempt(db_session, other_tenant_id, co, status=ApplicationStatus.READY, submitted_days_ago=None)
    _event(db_session, theirs, "READY", base + timedelta(minutes=1))
    _run(db_session, theirs, base + timedelta(minutes=2))
    _signal(db_session, other_tenant_id, "REJECTION", base + timedelta(minutes=3), application_id=theirs.id)

    events, _ = derive_events(db_session, tenant_id, cursor)

    assert events == []
    assert len(derive_events(db_session, other_tenant_id, cursor)[0]) == 3


def test_derivation_never_writes_to_the_database(db_session, tenant_id, attempt, cursor, base):  # noqa: F811
    _event(db_session, attempt, "READY", base + timedelta(minutes=1))
    _signal(db_session, tenant_id, "REJECTION", base + timedelta(minutes=2), application_id=attempt.id)

    derive_events(db_session, tenant_id, cursor)

    assert not db_session.new and not db_session.dirty and not db_session.deleted


# ---------------------------------------------------------------- privacy


def test_nothing_private_ever_reaches_the_notification_text(db_session, tenant_id, attempt, cursor, base):  # noqa: F811
    forbidden = [
        "Interview with Ribhu about the Platform role",
        "recruiter@employer.example",
        "employer.example",
        "We were impressed by your resume, please book a slot",
        "https://example.com/apply/secret",
        "I have 7 years of Python experience",
        settings.api_key,
    ]
    _signal(
        db_session,
        tenant_id,
        "INTERVIEW_INVITATION",
        base + timedelta(minutes=1),
        application_id=attempt.id,
        opportunity_id=attempt.opportunity_id,
        subject=forbidden[0],
        sender=forbidden[1],
        sender_domain=forbidden[2],
        excerpt=forbidden[3],
        payload={"answer": forbidden[5], "url": forbidden[4], "api_key": settings.api_key},
    )
    _run(db_session, attempt, base + timedelta(minutes=2), error_message=f"{forbidden[3]} at {forbidden[4]}")
    _status_set(db_session, attempt, ApplicationStatus.BLOCKED, blocked_reason="AUTH_REQUIRED")
    _event(db_session, attempt, "BLOCKED", base + timedelta(minutes=3), from_status="SUBMITTING", metadata={"reason": "AUTH_REQUIRED", "message": forbidden[3], "url": forbidden[4]})

    events, _ = derive_events(db_session, tenant_id, cursor)

    assert events
    for note in events:
        haystack = f"{note.title}\n{note.body}\n{note.path}\n{note.reason}"
        for secret in forbidden:
            assert secret not in haystack, (secret, haystack)
        assert len(note.body) <= notification_model.MAX_BODY
        assert "://" not in note.path


# ----------------------------------------------------------- cursor store


def test_the_cursor_survives_a_save_and_load_round_trip(tmp_path, monkeypatch, tenant_id):  # noqa: F811
    monkeypatch.setattr(settings, "desktop_state_dir", str(tmp_path / "state"))
    store = CursorStore()
    at = db_now()
    written = Cursor()
    written.start(at - timedelta(minutes=5))
    written.advance(APPLICATION_EVENTS, at, "event-1")

    store.save(tenant_id, written)
    read = store.load(tenant_id)

    assert read.watermark(APPLICATION_EVENTS) == at
    assert read.is_new(APPLICATION_EVENTS, at, "event-2")
    assert not read.is_new(APPLICATION_EVENTS, at, "event-1")
    assert (tmp_path / "state" / FILE_NAME).exists()


def test_a_missing_cursor_file_starts_at_now_so_history_is_not_replayed(db_session, tenant_id, attempt, tmp_path, monkeypatch, base):  # noqa: F811
    monkeypatch.setattr(settings, "desktop_state_dir", str(tmp_path / "state"))
    _event(db_session, attempt, "READY", base + timedelta(minutes=1))  # from before the desktop ever ran

    fresh = CursorStore().load(tenant_id)

    assert fresh.watermark(APPLICATION_EVENTS) is not None
    assert derive_events(db_session, tenant_id, fresh)[0] == []


def test_a_corrupt_cursor_file_warns_and_starts_fresh(tmp_path, monkeypatch, tenant_id, caplog):  # noqa: F811
    monkeypatch.setattr(settings, "desktop_state_dir", str(tmp_path / "state"))
    path = tmp_path / "state" / FILE_NAME
    path.parent.mkdir(parents=True)
    path.write_text("{not json at all", encoding="utf-8")

    with caplog.at_level("WARNING"):
        cursor = CursorStore().load(tenant_id)

    assert cursor.watermark(APPLICATION_EVENTS) is not None
    assert any("cursor" in record.message for record in caplog.records)


def test_saving_is_atomic_and_leaves_no_temp_files(tmp_path, monkeypatch, tenant_id, other_tenant_id):  # noqa: F811
    monkeypatch.setattr(settings, "desktop_state_dir", str(tmp_path / "state"))
    store = CursorStore()
    at = db_now()
    for tenant in (tenant_id, other_tenant_id):
        written = Cursor()
        written.start(at)
        store.save(tenant, written)

    files = sorted(p.name for p in (tmp_path / "state").iterdir())
    assert files == [FILE_NAME]
    payload = json.loads((tmp_path / "state" / FILE_NAME).read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert set(payload["tenants"]) == {tenant_id, other_tenant_id}
    # Nothing but watermarks and tie ids lives in the file.
    assert set(payload["tenants"][tenant_id]) <= {"application_events", "execution_runs", "signals"}
    for entry in payload["tenants"][tenant_id].values():
        assert set(entry) == {"at", "ids"}


def test_two_rows_in_one_clock_tick_are_told_apart(db_session, tenant_id, attempt, cursor, base):  # noqa: F811
    tick = base + timedelta(minutes=1)
    _status_set(db_session, attempt, ApplicationStatus.NEEDS_REVIEW)
    seen = _event(db_session, attempt, "NEEDS_REVIEW", tick, from_status="READY")
    unseen = _event(db_session, attempt, "SUBMITTED", tick, from_status="SUBMITTING")
    cursor.advance(APPLICATION_EVENTS, tick, seen.id)

    events, moved = derive_events(db_session, tenant_id, cursor)

    assert [e.kind for e in events] == [notification_model.SUBMISSION_COMPLETED]
    assert moved.watermark(APPLICATION_EVENTS) == tick
    assert not moved.is_new(APPLICATION_EVENTS, tick, unseen.id)
    assert derive_events(db_session, tenant_id, moved)[0] == []


# ---------------------------------------------------- dry run complete


def test_a_finished_dry_run_announces_itself_once_without_sensitive_details(db_session, tenant_id, attempt, cursor, base):  # noqa: F811
    run = _run(db_session, attempt, base + timedelta(minutes=5), status="DRY_RUN", outcome="DRY_RUN", error_class=None,
               error_message="SENTINEL-page-text https://jobs.example/apply?token=abc", application_url="https://jobs.example/apply?token=abc",
               diagnostics={"filled": {"email": "sentinel@example.com"}})
    events, moved = derive_events(db_session, tenant_id, cursor)
    dry = [e for e in events if e.kind == notification_model.DRY_RUN_COMPLETE]
    assert len(dry) == 1
    note = dry[0]
    assert note.title == "Dry run complete" and note.key == f"run:{run.id}:DRY_RUN"
    assert note.body == f"Dry run complete — {_company_name(db_session, attempt)} — {ROLE} — Ready for review"
    assert note.path == f"/desktop/applications/{attempt.id}" and note.importance == "normal"
    text = json.dumps(note.as_dict())
    assert "SENTINEL" not in text and "jobs.example" not in text and "token" not in text and "sentinel@" not in text
    assert not [e for e in events if e.kind == notification_model.EXECUTION_FAILED]
    # Deduplicated: the next poll with the moved cursor says nothing again.
    again, _ = derive_events(db_session, tenant_id, moved)
    assert [e for e in again if e.kind == notification_model.DRY_RUN_COMPLETE] == []


def test_a_dry_run_on_an_attempt_that_moved_on_is_not_announced(db_session, tenant_id, attempt, cursor, base):  # noqa: F811
    _run(db_session, attempt, base + timedelta(minutes=5), status="DRY_RUN", outcome="DRY_RUN", error_class=None)
    _status_set(db_session, attempt, ApplicationStatus.CANCELLED)
    events, _ = derive_events(db_session, tenant_id, cursor)
    assert [e for e in events if e.kind == notification_model.DRY_RUN_COMPLETE] == []


def test_another_tenants_dry_run_is_invisible(db_session, tenant_id, other_tenant_id, attempt, cursor, base):  # noqa: F811
    _run(db_session, attempt, base + timedelta(minutes=5), status="DRY_RUN", outcome="DRY_RUN", error_class=None)
    events, _ = derive_events(db_session, other_tenant_id, cursor)
    assert [e for e in events if e.kind == notification_model.DRY_RUN_COMPLETE] == []
