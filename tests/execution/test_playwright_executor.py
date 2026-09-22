"""The local Playwright executor through ExecutionService, against local fixtures."""

import pytest

from app.application.models import ApplicationStatus
from app.execution.executors.base import ExecutorError
from app.execution.models import (
    ErrorClass,
    ExecutionStatus,
    ExecutorKind,
    HandoffReason,
    VerificationStatus,
)
from app.execution.playwright import executor as executor_module
from app.pipeline.models import QueueState
from tests.execution.playwright_conftest import (
    GENERIC_BANK,
    LEVER_BANK,
    add_bank,
    attach,
    fixture_url,
    make_executor,
    make_session,
    requires_browser,
)

pytestmark = requires_browser


@pytest.fixture(scope="module")
def browser():
    session = make_session()
    yield session
    session.close()


@pytest.fixture
def resume_file(tmp_path):
    path = tmp_path / "resume.txt"
    path.write_text("Ribhu Siripurapu - resume (test file)\nPython, Go, FastAPI.\n", encoding="utf-8")
    return path


@pytest.fixture
def pw(browser, harness, resume_file):
    """Harness with a live (non-dry-run) Playwright executor attached."""
    executor = make_executor(browser, resume_file, dry_run=False)
    attach(harness, executor)
    harness.pw = executor
    return harness


def _run(h, attempt, worker="pw1"):
    return h.execute(attempt, worker=worker, executor=ExecutorKind.PLAYWRIGHT_LOCAL)


# ------------------------------------------------------------- submissions


def test_greenhouse_success_submits_once_and_verifies(pw, db_session):
    attempt = pw.ready(company="Acme GH", source="GREENHOUSE", application_url=fixture_url("greenhouse.html"))
    outcome = _run(pw, attempt)
    assert outcome["outcome"] == "SUBMITTED", outcome
    assert attempt.status == ApplicationStatus.VERIFIED.value and pw.pw.submit_clicks == 1
    run = pw.service.runs_for(attempt.id)[0]
    assert run.status == ExecutionStatus.VERIFIED.value and run.submit_invoked and run.executor_kind == "PLAYWRIGHT_LOCAL"
    assert run.verification_status == VerificationStatus.VERIFIED.value and run.verification_method == "application_id"
    assert run.confirmation_reference.startswith("GH-") and attempt.external_application_id == run.confirmation_reference
    assert "confirmation.html" in run.application_url
    assert run.diagnostics["strategy"] == "greenhouse" and run.diagnostics["fields_filled"] >= 6
    assert "password" not in str(run.diagnostics).lower()
    snapshot = pw.service._latest_snapshot(attempt.id)
    assert snapshot.executor_kind == "PLAYWRIGHT_LOCAL" and snapshot.field_count == 12
    assert pw.item(attempt).state == QueueState.SUCCEEDED.value


def test_lever_success_with_date_number_and_checkbox_group(pw, db_session):
    add_bank(db_session, pw.tenant_id, LEVER_BANK)
    pw.service._forget_cached_inputs()
    attempt = pw.ready(company="Beta LV", source="LEVER", application_url=fixture_url("lever.html"))
    outcome = _run(pw, attempt)
    unresolved = [(f.label, f.status.value, f.reason) for f in pw.service.preview(attempt.id).field_answers if f.status.value != "ANSWERED"]
    assert outcome["outcome"] == "SUBMITTED", (outcome, unresolved)
    run = pw.service.runs_for(attempt.id)[0]
    assert run.status == ExecutionStatus.VERIFIED.value and run.confirmation_reference.startswith("LV-")
    assert run.diagnostics["strategy"] == "lever"
    filled = set(run.diagnostics["filled"])
    assert {"Earliest start date ✱", "Years of professional experience ✱"} & filled or any("start date" in f.lower() for f in filled)
    assert any("interest" in f.lower() for f in filled)


def test_ashby_with_standard_controls_submits_and_custom_widgets_hand_off(pw, db_session):
    ok = pw.ready(company="Gamma AS", source="ASHBY", application_url=fixture_url("ashby.html"))
    outcome = _run(pw, ok)
    assert outcome["outcome"] == "SUBMITTED" and ok.status == ApplicationStatus.VERIFIED.value
    custom = pw.ready(company="Gamma Custom", source="ASHBY", application_url=fixture_url("ashby_custom.html"))
    outcome = _run(pw, custom, worker="pw2")
    assert outcome["outcome"] == "HANDOFF"
    assert custom.status == ApplicationStatus.BLOCKED.value and custom.blocked_reason == HandoffReason.UNSUPPORTED_FORM.value
    assert "UNSUPPORTED_FORM" in custom.status_reason and pw.pw.submit_clicks == 1


def test_generic_form_success_and_unknown_required_field(pw, db_session):
    add_bank(db_session, pw.tenant_id, GENERIC_BANK)
    pw.service._forget_cached_inputs()
    ok = pw.ready(company="Delta Web", source="CAREERS", application_url=fixture_url("generic.html"))
    outcome = _run(pw, ok)
    assert outcome["outcome"] == "SUBMITTED", outcome
    run = pw.service.runs_for(ok.id)[0]
    assert run.diagnostics["strategy"] == "generic" and run.status == ExecutionStatus.VERIFIED.value
    unknown = pw.ready(company="Delta Unknown", source="CAREERS", application_url=fixture_url("generic.html", mode="unknown"))
    outcome = _run(pw, unknown, worker="pw2")
    assert outcome["outcome"] == "HANDOFF" and unknown.blocked_reason == HandoffReason.UNKNOWN_REQUIRED_FIELD.value
    assert pw.pw.submit_clicks == 1


def test_validation_error_after_submit_needs_review(pw):
    attempt = pw.ready(company="Acme Invalid", source="GREENHOUSE", application_url=fixture_url("greenhouse.html", mode="validation"))
    outcome = _run(pw, attempt)
    assert outcome["outcome"] == "NEEDS_REVIEW"
    assert attempt.status == ApplicationStatus.NEEDS_REVIEW.value
    run = pw.service.runs_for(attempt.id)[0]
    assert run.error_class == ErrorClass.VALIDATION.value and run.diagnostics["validation_errors"]
    assert run.submit_invoked is True


def test_ambiguous_post_submit_state_is_unknown_and_never_resubmitted(pw):
    attempt = pw.ready(company="Acme Hang", source="GREENHOUSE", application_url=fixture_url("greenhouse.html", mode="hang"))
    outcome = _run(pw, attempt)
    assert outcome["outcome"] == "UNKNOWN"
    assert attempt.status == ApplicationStatus.UNCERTAIN.value and pw.pw.submit_clicks == 1
    run = pw.service.runs_for(attempt.id)[0]
    assert run.status == ExecutionStatus.UNKNOWN.value and run.error_class == ErrorClass.TIMEOUT.value
    item = pw.item(attempt)
    pw.service.queue.requeue(item, "impatient", "again")
    pw.session.commit()
    counts = pw.service.run_queue("pw9", limit=5, executor_kind=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert counts.get("awaiting_verification") == 1 and pw.pw.submit_clicks == 1
    verification = pw.service.verify(run.id, executor_kind=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert verification.verification_status in (VerificationStatus.UNKNOWN.value, VerificationStatus.PENDING.value)


def test_plain_thanks_page_is_likely_not_verified(pw):
    attempt = pw.ready(company="Acme Plain", source="GREENHOUSE", application_url=fixture_url("greenhouse.html", mode="plain"))
    outcome = _run(pw, attempt)
    assert outcome["outcome"] == "SUBMITTED"
    run = pw.service.runs_for(attempt.id)[0]
    assert run.verification_status == VerificationStatus.LIKELY.value and run.verification_method == "redirect_url"
    assert attempt.status == ApplicationStatus.SUBMITTED.value and attempt.verified_at is None


def test_captcha_after_submit_hands_off(pw):
    attempt = pw.ready(company="Acme Captcha2", source="GREENHOUSE", application_url=fixture_url("greenhouse.html", mode="captcha"))
    outcome = _run(pw, attempt)
    assert outcome["outcome"] == "HANDOFF" and attempt.blocked_reason == HandoffReason.CAPTCHA_REQUIRED.value
    run = pw.service.runs_for(attempt.id)[0]
    assert run.handoff["stopped_at"] == "after_submit" and run.submit_invoked is True


# ---------------------------------------------------------------- handoffs


@pytest.mark.parametrize("page, reason", [("captcha.html", HandoffReason.CAPTCHA_REQUIRED), ("login.html", HandoffReason.AUTH_REQUIRED), ("mfa.html", HandoffReason.MFA_REQUIRED), ("empty.html", HandoffReason.AMBIGUOUS_FORM)])
def test_human_verification_pages_hand_off_before_any_fill(pw, page, reason):
    attempt = pw.ready(company=f"Site {page}", source="CAREERS", application_url=fixture_url(page))
    outcome = _run(pw, attempt)
    assert outcome["outcome"] == "HANDOFF", outcome
    assert attempt.status == ApplicationStatus.BLOCKED.value and attempt.blocked_reason == reason.value
    run = pw.service.runs_for(attempt.id)[0]
    assert run.status == ExecutionStatus.HANDOFF.value and run.submit_invoked is True and pw.pw.submit_clicks == 0
    assert run.handoff["remaining_steps"] and page.split(".")[0] in run.diagnostics["handoff_page"]
    assert "page:" in attempt.status_reason
    assert attempt.released_at is None, "the slot is kept while the person finishes"


def test_generated_pdf_is_what_the_form_receives(browser, harness, db_session, tmp_path, monkeypatch):
    """Phase 8: no personal file configured; the preparation's rendered PDF is uploaded."""
    from app.config import settings
    from app.documents.database.models import DocumentArtifactRow

    monkeypatch.setattr(settings, "documents_root", str(tmp_path / "documents"))
    executor = make_executor(browser, None, dry_run=False)
    attach(harness, executor)
    attempt = harness.ready(company="Acme Rendered", source="GREENHOUSE", application_url=fixture_url("greenhouse.html"))
    outcome = harness.execute(attempt, executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert outcome["outcome"] == "SUBMITTED", outcome
    run = harness.service.runs_for(attempt.id)[0]
    uploads = {u["artifact"]: u["file"] for u in run.diagnostics["uploads"]}
    assert uploads["RESUME"] == "resume-v1.pdf" and uploads["COVER_LETTER"] == "cover-letter-v1.pdf"
    artifact = db_session.get(DocumentArtifactRow, run.resume_artifact_id)
    assert artifact.format == "PDF" and artifact.content_hash == run.diagnostics["artifacts"]["RESUME"]["sha256"]
    assert (tmp_path / "documents" / artifact.relative_path).read_bytes()[:4] == b"%PDF"


def test_tampered_or_missing_rendered_file_is_not_uploaded(browser, harness, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "documents_root", str(tmp_path / "documents"))
    executor = make_executor(browser, None, dry_run=False)
    attach(harness, executor)
    attempt = harness.ready(company="Acme Tampered", source="GREENHOUSE", application_url=fixture_url("greenhouse.html"))
    (mine,) = harness.service.claim("w1", limit=1)
    run, package, _ = harness.service.start(mine, "w1", ExecutorKind.PLAYWRIGHT_LOCAL)
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.SUBMITTING.value and run.resume_artifact_id
    path = package.execution_config["artifact_files"]["RESUME"]
    with open(path, "ab") as handle:
        handle.write(b"\n%tampered after verification")
    assert executor._artifact_path("RESUME", package) is None, "hash mismatch immediately before upload"
    import os

    os.unlink(path)
    assert executor._artifact_path("RESUME", package) is None
    harness.service.queue.release(mine, "w1", "test cleanup")
    db_session = harness.session
    db_session.commit()


def test_empty_or_missing_file_is_never_uploaded(browser, harness, tmp_path):
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    executor = make_executor(browser, empty, dry_run=False)
    assert executor._artifact_path("RESUME") is None
    executor.artifact_files["RESUME"] = str(tmp_path / "nope.pdf")
    assert executor._artifact_path("RESUME") is None
    assert executor._artifact_path("RESUME", None) is None


def test_cap_rollover_at_the_pre_submit_gate_refreshes_the_reservation(pw, db_session):
    """Admitted 'Friday', executed 'Saturday': the gate takes today's slot and releases Friday's."""
    from datetime import timedelta

    from app.core.timeutils import utc_now
    from app.scheduler.caps import period_keys

    attempt = pw.ready(company="Acme Rollover", source="GREENHOUSE", application_url=fixture_url("greenhouse.html"))
    ledger = pw.service.attempts.ledger
    friday = period_keys(utc_now() - timedelta(days=8), "UTC")
    ledger.release(attempt.cap_day, attempt.cap_week)
    ledger._ensure("DAY", friday.day.key)
    ledger._ensure("WEEK", friday.week.key)
    ledger._increment("DAY", friday.day.key, None)
    ledger._increment("WEEK", friday.week.key, None)
    today = period_keys(utc_now(), "UTC")
    original = pw.service._pre_submit_gate

    def gate_after_rollover(application_id):
        inner = original(application_id)

        def gate():
            row = pw.service.get_attempt(application_id)
            row.cap_day, row.cap_week = friday.day.key, friday.week.key  # start() saw today; the period "rolled over"
            db_session.flush()
            return inner()

        return gate

    pw.service._pre_submit_gate = gate_after_rollover
    outcome = _run(pw, attempt)
    assert outcome["outcome"] == "SUBMITTED" and attempt.status == ApplicationStatus.VERIFIED.value
    assert attempt.cap_day == today.day.key and attempt.cap_week == today.week.key
    assert ledger.usage("DAY", today.day.key) == 1 and ledger.usage("DAY", friday.day.key) == 0, "refreshed, not double-counted"


def test_cap_exhausted_at_the_gate_waits_instead_of_submitting(pw, db_session):
    from datetime import timedelta

    from app.core.timeutils import utc_now
    from app.scheduler.caps import period_keys
    from tests.execution.conftest import set_policy

    attempt = pw.ready(company="Acme Full", source="GREENHOUSE", application_url=fixture_url("greenhouse.html"))
    friday = period_keys(utc_now() - timedelta(days=8), "UTC")
    today = period_keys(utc_now(), "UTC")
    ledger = pw.service.attempts.ledger
    set_policy(db_session, pw.tenant_id, daily_cap=1)
    original = pw.service._pre_submit_gate

    def gate_after_rollover(application_id):
        inner = original(application_id)

        def gate():
            row = pw.service.get_attempt(application_id)
            ledger.release(row.cap_day, row.cap_week)
            row.cap_day, row.cap_week = friday.day.key, friday.week.key
            ledger._ensure("DAY", today.day.key)
            ledger._increment("DAY", today.day.key, None)  # someone else took today's only slot
            db_session.flush()
            return inner()

        return gate

    pw.service._pre_submit_gate = gate_after_rollover
    outcome = _run(pw, attempt)
    assert outcome["outcome"] == "NEEDS_REVIEW" and pw.pw.submit_clicks == 0
    assert attempt.status == ApplicationStatus.READY.value, "waits for capacity; not a failure, not a review"
    item = pw.item(attempt)
    assert item.state == QueueState.PENDING.value and item.available_at >= today.day.ends_at.replace(tzinfo=None) - timedelta(seconds=1)
    assert ledger.usage("DAY", today.day.key) == 1, "nothing double-counted"


# ----------------------------------------------------------- crash recovery


def test_browser_crash_before_submit_is_retryable(pw, monkeypatch):
    attempt = pw.ready(company="Acme Crash1", source="GREENHOUSE", application_url=fixture_url("greenhouse.html"))

    def boom(self, page, field, answer, package=None):
        raise RuntimeError("Target page, context or browser has been closed")

    monkeypatch.setattr(executor_module.PlaywrightExecutor, "_fill", boom)
    outcome = _run(pw, attempt)
    assert outcome["outcome"] == "RETRYABLE_FAILURE"
    assert attempt.status == ApplicationStatus.READY.value and pw.item(attempt).state == QueueState.RETRY_WAIT.value
    run = pw.service.runs_for(attempt.id)[0]
    assert run.error_class == ErrorClass.BROWSER_CRASH.value and run.submit_invoked is False and pw.pw.submit_clicks == 0
    assert pw.session_running_again()


def test_browser_crash_after_submit_is_unknown(pw, monkeypatch):
    attempt = pw.ready(company="Acme Crash2", source="GREENHOUSE", application_url=fixture_url("greenhouse.html"))
    original = executor_module.scan_page
    calls = {"n": 0}

    def crash_after_click(page):
        calls["n"] += 1
        if calls["n"] >= 3:  # initial scan, rescan, then the post-submit poll dies
            raise RuntimeError("Target crashed")
        return original(page)

    monkeypatch.setattr(executor_module, "scan_page", crash_after_click)
    outcome = _run(pw, attempt)
    assert outcome["outcome"] == "UNKNOWN" and attempt.status == ApplicationStatus.UNCERTAIN.value
    run = pw.service.runs_for(attempt.id)[0]
    assert run.error_class == ErrorClass.BROWSER_CRASH.value and run.submit_invoked and pw.pw.submit_clicks == 1
    assert pw.item(attempt).state == QueueState.NEEDS_REVIEW.value


def test_navigation_failures_are_classified(browser, harness):
    executor = make_executor(browser, None, dry_run=False)
    attach(harness, executor)
    gone = harness.ready(company="Gone Inc", source="CAREERS", application_url=fixture_url("does_not_exist.html"))
    outcome = harness.execute(gone, executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert outcome["outcome"] == "PERMANENT_FAILURE" and gone.status == ApplicationStatus.FAILED.value
    run = harness.service.runs_for(gone.id)[0]
    assert run.error_class == ErrorClass.TARGET_GONE.value
    with pytest.raises(ExecutorError) as excinfo:
        raise executor._classify_navigation(RuntimeError("Timeout 30000ms exceeded"))
    assert excinfo.value.retryable and excinfo.value.error_class is ErrorClass.TIMEOUT and excinfo.value.before_submit


def test_pre_submit_gate_stops_the_click_when_policy_changed(pw, db_session):
    from tests.execution.conftest import set_policy

    attempt = pw.ready(company="Acme Gate", source="GREENHOUSE", application_url=fixture_url("greenhouse.html"))
    original_gate = pw.service._pre_submit_gate

    def gate_with_policy_change(application_id):
        inner = original_gate(application_id)

        def gate():
            set_policy(db_session, pw.tenant_id, blocked_companies=[pw.opportunities.jobs.company("Acme Gate")])
            return inner()

        return gate

    pw.service._pre_submit_gate = gate_with_policy_change
    outcome = _run(pw, attempt)
    assert outcome["outcome"] == "NEEDS_REVIEW" and pw.pw.submit_clicks == 0
    assert "COMPANY_BLOCKED" in attempt.status_reason
    run = pw.service.runs_for(attempt.id)[0]
    assert run.diagnostics["gate_failures"] and run.error_class == ErrorClass.POLICY.value


# ----------------------------------------------------------------- dry run


def test_dry_run_never_submits_and_reports_mapping(browser, harness, resume_file):
    executor = make_executor(browser, resume_file, dry_run=True)
    attach(harness, executor)
    attempt = harness.ready(company="Acme Dry", source="GREENHOUSE", application_url=fixture_url("greenhouse.html"))
    outcome = harness.execute(attempt, executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert outcome["outcome"] == "DRY_RUN" and executor.submit_clicks == 0
    assert attempt.status == ApplicationStatus.READY.value and attempt.submission_key is None
    run = harness.service.runs_for(attempt.id)[0]
    assert run.status == ExecutionStatus.DRY_RUN.value and run.submit_invoked is False
    assert run.diagnostics["dry_run"] is True and run.diagnostics["fields_filled"] >= 6 and run.diagnostics["would_submit_with"]
    assert harness.item(attempt).state == QueueState.NEEDS_REVIEW.value
    # The same attempt can go live afterwards: one submit, nothing double-counted.
    executor.dry_run = False
    harness.service.retry(attempt.id, "user", "looked fine")
    harness.execute(attempt, worker="pw-live", executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert attempt.status == ApplicationStatus.VERIFIED.value and executor.submit_clicks == 1
    assert [r.status for r in harness.service.runs_for(attempt.id)] == [ExecutionStatus.VERIFIED.value, ExecutionStatus.DRY_RUN.value]


def test_manual_confirmation_after_handoff_still_works_with_playwright(pw):
    attempt = pw.ready(company="Site Captcha3", source="CAREERS", application_url=fixture_url("captcha.html"))
    _run(pw, attempt)
    run = pw.service.runs_for(attempt.id)[0]
    pw.service.confirm(run.id, submitted=True, reference="BY-HAND-1")
    pw.refresh(attempt)
    assert attempt.status == ApplicationStatus.VERIFIED.value and attempt.confirmation == "BY-HAND-1"


# ------------------------------------------- Phase 13: embeds and SPAs


def test_form_inside_an_embedded_ats_iframe_is_discovered_and_filled(browser, harness, resume_file):
    """Employer career sites commonly embed the ATS form in an iframe that is
    injected after load (Greenhouse embeds seen on real boards in Phase 13)."""
    executor = make_executor(browser, resume_file, dry_run=True, settle_ms=4000)
    attach(harness, executor)
    attempt = harness.ready(company="Echo Embed", source="GREENHOUSE", application_url=fixture_url("embed_host.html"))
    outcome = harness.execute(attempt, executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert outcome["outcome"] == "DRY_RUN", outcome
    run = harness.service.runs_for(attempt.id)[0]
    assert run.diagnostics["in_frame"] is True and run.diagnostics["fields_filled"] >= 6 and run.diagnostics["would_submit_with"]
    snapshot = harness.service._latest_snapshot(attempt.id)
    assert snapshot.field_count == 12 and "greenhouse.html" in snapshot.source_url
    # and the same form submits through the frame when live
    executor.dry_run = False
    harness.service.retry(attempt.id, "user", "looked fine")
    harness.execute(attempt, worker="pw-live", executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert attempt.status == ApplicationStatus.VERIFIED.value and executor.submit_clicks == 1


def test_client_rendered_form_is_found_after_settling(browser, harness, resume_file):
    executor = make_executor(browser, resume_file, dry_run=True, settle_ms=4000)
    attach(harness, executor)
    attempt = harness.ready(company="Foxtrot SPA", source="ASHBY", application_url=fixture_url("spa.html", delay=1500))
    outcome = harness.execute(attempt, executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert outcome["outcome"] == "DRY_RUN", outcome
    run = harness.service.runs_for(attempt.id)[0]
    assert run.diagnostics["fields_discovered"] == 3 and run.diagnostics["in_frame"] is False
    assert 1000 <= run.diagnostics["settle_ms"] <= 4000


def test_page_that_never_renders_a_form_hands_off_after_the_settle_window(browser, harness, resume_file):
    executor = make_executor(browser, resume_file, dry_run=True, settle_ms=1200)
    attach(harness, executor)
    attempt = harness.ready(company="Foxtrot Slow", source="ASHBY", application_url=fixture_url("spa.html", delay=20000))
    outcome = harness.execute(attempt, executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert outcome["outcome"] == "HANDOFF", outcome
    run = harness.service.runs_for(attempt.id)[0]
    assert run.handoff_reason == HandoffReason.AMBIGUOUS_FORM.value and executor.submit_clicks == 0


def test_cookie_banner_buttons_do_not_count_as_a_form(browser, harness, resume_file):
    executor = make_executor(browser, resume_file, dry_run=True, settle_ms=500)
    attach(harness, executor)
    attempt = harness.ready(company="Banner Only", source="LEVER", application_url=fixture_url("banner_only.html"))
    outcome = harness.execute(attempt, executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert outcome["outcome"] == "HANDOFF", outcome
    assert harness.service.runs_for(attempt.id)[0].handoff_reason == HandoffReason.AMBIGUOUS_FORM.value


def test_a_listing_page_with_only_a_search_form_is_not_an_application(browser, harness, resume_file):
    """A closed Greenhouse posting redirects to the board index (Phase 13):
    its search form must never be filled or 'submitted'."""
    executor = make_executor(browser, resume_file, dry_run=False, settle_ms=500)
    attach(harness, executor)
    attempt = harness.ready(company="Hotel Gone", source="GREENHOUSE", application_url=fixture_url("search_only.html"))
    outcome = harness.execute(attempt, executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert outcome["outcome"] == "HANDOFF", outcome
    run = harness.service.runs_for(attempt.id)[0]
    assert run.handoff_reason == HandoffReason.AMBIGUOUS_FORM.value and "not an application form" in run.error_message and executor.submit_clicks == 0


def test_inline_captcha_completed_by_the_person_lets_the_run_continue(browser, harness, resume_file):
    """Lever's hCaptcha and Greenhouse's reCAPTCHA sit inside the form (Phase 13).
    Unsolved: hand off before typing anything. Solved by a person (the
    provider's response token is present): the run proceeds. Nothing solves it."""
    executor = make_executor(browser, resume_file, dry_run=True, settle_ms=500)
    attach(harness, executor)
    unsolved = harness.ready(company="Epsilon Widget", source="CAREERS", application_url=fixture_url("captcha.html"))
    outcome = harness.execute(unsolved, executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert outcome["outcome"] == "HANDOFF" and unsolved.blocked_reason == HandoffReason.CAPTCHA_REQUIRED.value
    run = harness.service.runs_for(unsolved.id)[0]
    assert "human verification widget" in run.error_message and run.diagnostics.get("fields_filled") is None
    solved = harness.ready(company="Epsilon Solved", source="CAREERS", application_url=fixture_url("captcha.html", solved=1))
    outcome = harness.execute(solved, executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert outcome["outcome"] == "DRY_RUN", outcome
    assert harness.service.runs_for(solved.id)[0].diagnostics["fields_filled"] == 1 and executor.submit_clicks == 0


def test_invisible_recaptcha_badge_is_not_a_wall_but_a_visible_challenge_is(browser, harness, resume_file):
    """Greenhouse-hosted boards carry an invisible reCAPTCHA badge (Phase 13): the
    provider scores the submit itself, there is nothing for a person to
    complete up front, so the run proceeds; a visible widget still hands off,
    and a challenge that appears after the click hands off after the click."""
    executor = make_executor(browser, resume_file, dry_run=True, settle_ms=500)
    attach(harness, executor)
    badge = harness.ready(company="Acme Badge", source="GREENHOUSE", application_url=fixture_url("greenhouse.html", badge=1))
    outcome = harness.execute(badge, executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert outcome["outcome"] == "DRY_RUN", outcome
    run = harness.service.runs_for(badge.id)[0]
    assert run.diagnostics["captcha_invisible"] is True and run.diagnostics["fields_filled"] >= 6 and executor.submit_clicks == 0
    wall = harness.ready(company="Acme Wall", source="CAREERS", application_url=fixture_url("captcha.html"))
    assert harness.execute(wall, executor=ExecutorKind.PLAYWRIGHT_LOCAL)["outcome"] == "HANDOFF"
