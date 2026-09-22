"""Increment 4: user-controlled real submission from the desktop review page.

The invariant under test everywhere in this file: **one explicit user action
executes exactly one existing READY attempt through the existing pipeline.**
The desktop layer adds no gate of its own and removes none — every block
(preparation missing or stale, closed opening, blocked company, duplicate,
cap, kill switch, missing answer, CAPTCHA / MFA / unsupported form, a lease
held by the extension) is the one ``ExecutionService`` already enforces, and
is asserted here through the desktop route. Nothing batches, nothing retries
by itself, nothing resubmits an UNKNOWN attempt.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.application.killswitch import set_paused
from app.application.models import ApplicationStatus
from app.config import settings
from app.core.errors import ConflictError
from app.desktop import runner as runner_module
from app.desktop.runner import get_runner, reset_runner
from app.execution.executors import mock as m
from app.execution.executors.mock import MockExecutor
from app.execution.models import (
    ExecutionOutcome,
    ExecutionResult,
    ExecutorKind,
    HandoffReason,
)
from app.execution.service import ExecutionService
from app.main import app
from app.pipeline.database.models import OpportunityRow
from app.pipeline.models import ApplicationPolicyUpdate, QueueState
from app.pipeline.repository import PolicyRepository
from app.security import DASHBOARD_COOKIE, DESKTOP_HEADER, DESKTOP_HEADER_VALUE
from tests.execution.conftest import harness  # noqa: F401
from tests.execution.playwright_conftest import (
    fixture_url,
    make_executor,
    make_session,
    requires_browser,
)
from tests.preparation.conftest import OpportunityFactory
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

COOKIE = {DASHBOARD_COOKIE: settings.api_key}
DESKTOP = {DESKTOP_HEADER: DESKTOP_HEADER_VALUE}
RUN_URL = "/desktop/api/attempts/{}/run"


# ------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def _fresh_runner():
    reset_runner()
    yield
    reset_runner()


@pytest.fixture(autouse=True)
def _live_submission_enabled(tenant_id):  # noqa: F811
    """This module is about what happens *after* the person switched to LIVE,
    so it takes the real switch; SAFE refusals live in test_submission_mode_safety.py."""
    from app.execution import submission_mode

    submission_mode.enable_live(tenant_id, actor="test")


@pytest.fixture
def client(tenant_id):  # noqa: F811
    app.dependency_overrides[get_tenant_id] = lambda: tenant_id
    try:
        yield TestClient(app, follow_redirects=False, cookies=COOKIE)
    finally:
        app.dependency_overrides.pop(get_tenant_id, None)


@pytest.fixture
def mock_factory(monkeypatch, harness):  # noqa: F811
    """Every desktop run in this module builds the harness' MockExecutor.

    The real factory builds a headed ``PlaywrightExecutor``; only the browser
    test below exercises that (with a local ``file://`` fixture).
    """
    monkeypatch.setattr(runner_module, "default_executor_factory", lambda mode: harness.mock)
    return harness.mock


@pytest.fixture
def ready(harness, db_session):  # noqa: F811
    attempt = harness.ready(company="Desktop Corp", title="Platform Engineer", fit_score=88)
    db_session.commit()
    return attempt


@pytest.fixture(scope="module")
def browser():
    """Availability probe for the one real-browser test.

    Each desktop run launches its **own** session inside its own thread:
    Playwright's sync API belongs to the thread that started it, and the run
    thread is not this one.
    """
    session = make_session()
    yield session
    session.close()


@pytest.fixture
def resume_file(tmp_path):
    path = tmp_path / "resume.txt"
    path.write_text("Ribhu Siripurapu - resume (test file)\nPython, Go, FastAPI.\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------- helpers


def _wait(job, timeout: float = 60.0):
    """Block until the run thread settles the job; never longer than ``timeout``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not job.running:
            return job
        time.sleep(0.02)
    raise AssertionError(f"desktop run {job.id} never finished: {job.as_dict()}")


def _post(client, application_id, mode="live", confirm="SUBMIT", headers=DESKTOP):
    return client.post(RUN_URL.format(application_id), json={"mode": mode, "confirm": confirm}, headers=headers)


def _run(client, application_id, mode="live", confirm="SUBMIT"):
    """One explicit user action, waited out. Returns the finished job."""
    response = _post(client, application_id, mode=mode, confirm=confirm)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["application_id"] == application_id and body["mode"] == mode
    assert body["job_url"] == f"/desktop/applications/{application_id}/run/{body['job_id']}"
    job = get_runner().get(body["job_id"])
    assert job is not None
    return _wait(job)


def _reload(harness, *rows):  # noqa: F811
    """See what the run thread committed from its own session."""
    harness.session.rollback()
    for row in rows:
        harness.session.refresh(row)
    return rows[0] if len(rows) == 1 else rows


class _UnsupportedFormExecutor(MockExecutor):
    """A page whose widgets cannot be driven safely: hand off, never guess."""

    def execute(self, package, form, answers, gate=None) -> ExecutionResult:
        return ExecutionResult(
            outcome=ExecutionOutcome.HANDOFF,
            handoff_reason=HandoffReason.UNSUPPORTED_FORM,
            stopped_at="before_submit",
            remaining_steps=["complete the custom widgets by hand", "press submit"],
            message="the page uses widgets this executor cannot drive safely",
        )


class _ModeAwareMock(MockExecutor):
    """Mimics the real executor's mode contract: a dry run never presses submit."""

    def __init__(self, dry_run: bool, **kwargs):
        super().__init__(**kwargs)
        self.dry_run = dry_run

    def execute(self, package, form, answers, gate=None) -> ExecutionResult:
        if self.dry_run:
            if gate is not None:
                gate()
            return ExecutionResult(outcome=ExecutionOutcome.DRY_RUN, message="dry run: submit deliberately not pressed", diagnostics={"fields_filled": len(answers)})
        return super().execute(package, form, answers, gate=gate)


# ------------------------------------------------------- 1. the happy path


def test_one_explicit_action_executes_exactly_one_ready_attempt(client, harness, ready, mock_factory):  # noqa: F811
    job = _run(client, ready.id)
    assert job.state == "finished", job.as_dict()
    assert job.outcome["outcome"] == ExecutionOutcome.SUBMITTED.value, job.as_dict()
    assert job.run_id and job.finished_at is not None and job.mode == "live"
    runs = harness.service.runs_for(ready.id)
    assert len(runs) == 1, "one action, one execution run"
    assert runs[0].id == job.run_id and runs[0].submit_invoked is True
    assert mock_factory.submit_calls[ready.id] == 1, "exactly one submit was pressed"
    _reload(harness, ready)
    assert ready.status == ApplicationStatus.VERIFIED.value
    # the job is readable back through its own route
    status = client.get(f"/desktop/api/runs/{job.id}")
    assert status.status_code == 200 and status.json()["state"] == "finished" and status.json()["run_id"] == job.run_id


# ------------------------------------------------------ 2. not READY at all


def test_a_non_ready_attempt_is_refused_by_the_route_and_by_the_runner(client, harness, ready, db_session, mock_factory):  # noqa: F811
    ready.status = ApplicationStatus.CANCELLED.value
    db_session.commit()
    response = _post(client, ready.id)
    assert response.status_code == 409 and "CANCELLED" in response.json()["detail"]
    assert get_runner().jobs_for(ready.id) == [], "the route never started a job"
    # bypassing the route does not bypass the check either: the job fails, nothing executes
    job = _wait(get_runner().start(harness.tenant_id, ready.id, "live", executor_factory=lambda mode: harness.mock))
    assert job.state == "failed" and "not READY" in job.error
    assert harness.service.runs_for(ready.id) == [] and mock_factory.submit_calls[ready.id] == 0


# ------------------------------------------------------- 3.-9. the gates


def test_missing_preparation_blocks_before_any_submit(client, harness, ready, db_session, mock_factory):  # noqa: F811
    ready.preparation_id = None
    db_session.commit()
    job = _run(client, ready.id)
    assert job.state == "finished" and job.outcome["outcome"] == "precondition_failed"
    assert "PREPARATION_MISSING" in job.outcome["codes"]
    _reload(harness, ready)
    assert ready.status == ApplicationStatus.NEEDS_REVIEW.value
    assert mock_factory.submit_calls[ready.id] == 0


def test_stale_preparation_blocks_before_any_submit(client, harness, ready, db_session, mock_factory):  # noqa: F811
    from app.career.repository import EvidenceRepository

    # exactly how the hardening suite makes a package stale: the inputs move
    EvidenceRepository(db_session, harness.tenant_id).upsert_profile({"phone": "+91 99999 99999"}, None, "test")
    db_session.commit()
    job = _run(client, ready.id)
    assert job.outcome["outcome"] == "precondition_failed" and "PREPARATION_STALE" in job.outcome["codes"]
    _reload(harness, ready)
    assert ready.status == ApplicationStatus.PREPARING.value, "re-prepared, never submitted"
    assert mock_factory.submit_calls[ready.id] == 0


def test_closed_opening_blocks_before_any_submit(client, harness, ready, db_session, mock_factory):  # noqa: F811
    db_session.get(OpportunityRow, ready.opportunity_id).status = "CLOSED"
    db_session.commit()
    job = _run(client, ready.id)
    assert job.outcome["outcome"] == "precondition_failed" and "OPPORTUNITY_CLOSED" in job.outcome["codes"]
    _reload(harness, ready)
    assert ready.status == ApplicationStatus.CLOSED.value and mock_factory.submit_calls[ready.id] == 0


def test_blocked_company_blocks_before_any_submit(client, harness, ready, db_session, mock_factory):  # noqa: F811
    PolicyRepository(db_session, harness.tenant_id).update(ApplicationPolicyUpdate(blocked_companies=[harness.opportunities.jobs.company("Desktop Corp")]), "test")
    db_session.commit()
    job = _run(client, ready.id)
    assert job.outcome["outcome"] == "precondition_failed" and "COMPANY_BLOCKED" in job.outcome["codes"]
    _reload(harness, ready)
    assert ready.status == ApplicationStatus.CLOSED.value and mock_factory.submit_calls[ready.id] == 0


def test_duplicate_application_blocks_before_any_submit(client, harness, ready, db_session, mock_factory):  # noqa: F811
    other = harness.opportunities.make(title="Platform Engineer", company="Desktop Corp", fit_score=70, location="Berlin", remote_type="ONSITE")
    fake_attempt(db_session, harness.tenant_id, other, ApplicationStatus.SUBMITTED, submitted_days_ago=1)
    set_policy(db_session, harness.tenant_id, cooldown_days=0)
    job = _run(client, ready.id)
    assert job.outcome["outcome"] == "precondition_failed" and "DUPLICATE_APPLICATION" in job.outcome["codes"]
    _reload(harness, ready)
    assert ready.status == ApplicationStatus.CLOSED.value and mock_factory.submit_calls[ready.id] == 0


def test_exhausted_cap_waits_instead_of_submitting(client, harness, ready, db_session, mock_factory):  # noqa: F811
    set_policy(db_session, harness.tenant_id, daily_cap=0)
    job = _run(client, ready.id)
    assert job.outcome["outcome"] == "precondition_failed" and "DAILY_CAP_REACHED" in job.outcome["codes"]
    _reload(harness, ready)
    assert ready.status == ApplicationStatus.READY.value, "a full cap is a wait, not a failure"
    assert harness.item(ready).state == QueueState.PENDING.value
    assert mock_factory.submit_calls[ready.id] == 0


def test_kill_switch_stops_the_desktop_run(client, harness, ready, db_session, mock_factory):  # noqa: F811
    set_paused(db_session, True, "test")
    try:
        job = _run(client, ready.id)
    finally:
        set_paused(db_session, False)
    assert job.state == "finished" and job.outcome["outcome"] == "paused"
    _reload(harness, ready)
    assert ready.status == ApplicationStatus.READY.value
    assert harness.service.runs_for(ready.id) == [] and mock_factory.submit_calls[ready.id] == 0


# ------------------------------------------------ 10.-13. handoffs, no click


def test_missing_required_answer_needs_the_candidate_not_a_guess(client, harness, ready, mock_factory):  # noqa: F811
    mock_factory.default = m.NEEDS_INPUT
    job = _run(client, ready.id)
    assert job.outcome["outcome"] == ExecutionOutcome.NEEDS_USER_INPUT.value
    _reload(harness, ready)
    assert ready.status == ApplicationStatus.NEEDS_USER_INPUT.value
    assert mock_factory.submit_calls[ready.id] == 0


@pytest.mark.parametrize(
    "scripted_outcome,reason",
    [(m.CAPTCHA, HandoffReason.CAPTCHA_REQUIRED), (m.MFA, HandoffReason.MFA_REQUIRED)],
)
def test_captcha_and_mfa_hand_off_without_a_single_click(client, harness, ready, mock_factory, scripted_outcome, reason):  # noqa: F811
    mock_factory.default = scripted_outcome
    job = _run(client, ready.id)
    assert job.outcome["outcome"] == ExecutionOutcome.HANDOFF.value
    _reload(harness, ready)
    assert ready.status == ApplicationStatus.BLOCKED.value and ready.blocked_reason == reason.value
    assert mock_factory.submit_calls[ready.id] == 0
    assert harness.service.runs_for(ready.id)[0].handoff_reason == reason.value


def test_unsupported_form_hands_off_without_a_single_click(harness, ready, monkeypatch):  # noqa: F811
    executor = _UnsupportedFormExecutor()
    monkeypatch.setattr(runner_module, "default_executor_factory", lambda mode: executor)
    job = _wait(get_runner().start(harness.tenant_id, ready.id, "live"))
    assert job.state == "finished" and job.outcome["outcome"] == ExecutionOutcome.HANDOFF.value
    _reload(harness, ready)
    assert ready.status == ApplicationStatus.BLOCKED.value and ready.blocked_reason == HandoffReason.UNSUPPORTED_FORM.value
    assert executor.submit_calls[ready.id] == 0


# ------------------------------------------ 14. the real browser, dry run


@requires_browser
def test_dry_run_through_the_real_playwright_executor_never_clicks(browser, harness, db_session, resume_file):  # noqa: F811
    from app.execution.playwright.browser import BrowserSession

    attempt = harness.ready(company="Dry Run Co", source="GREENHOUSE", application_url=fixture_url("greenhouse.html"))
    db_session.commit()
    built: list = []

    def factory(mode):
        # Built inside the run thread: the browser belongs to the thread that starts it.
        executor = make_executor(BrowserSession(headless=True, profile_dir=""), resume_file, dry_run=(mode == "dry_run"))
        built.append(executor)
        return executor

    job = _wait(get_runner().start(harness.tenant_id, attempt.id, "dry_run", executor_factory=factory), timeout=180)
    assert job.state == "finished", job.as_dict()
    assert job.outcome["outcome"] == ExecutionOutcome.DRY_RUN.value, job.as_dict()
    assert built and built[0].submit_clicks == 0, "a dry run never presses submit"
    run = harness.service.runs_for(attempt.id)[0]
    assert run.submit_invoked is False and run.executor_kind == ExecutorKind.PLAYWRIGHT_LOCAL.value
    _reload(harness, attempt)
    assert attempt.status == ApplicationStatus.READY.value, "a dry run leaves the attempt submittable"


# ------------------------------------------------- 15. exactly one attempt


def test_only_the_chosen_attempt_is_executed(client, harness, ready, db_session, mock_factory):  # noqa: F811
    other = harness.ready(company="Untouched Ltd", title="Data Engineer")
    db_session.commit()
    job = _run(client, ready.id)
    assert job.outcome["outcome"] == ExecutionOutcome.SUBMITTED.value
    assert len(harness.service.runs_for(ready.id)) == 1
    assert harness.service.runs_for(other.id) == [], "no other attempt was touched"
    _reload(harness, ready, other)
    assert other.status == ApplicationStatus.READY.value
    assert mock_factory.submit_calls[other.id] == 0 and mock_factory.submit_calls[ready.id] == 1
    assert len(get_runner().jobs_for(other.id)) == 0


# --------------------------------------------- 16.-18. unknown and crashes


def test_unknown_after_the_click_is_uncertain_and_never_resubmitted(client, harness, ready, mock_factory):  # noqa: F811
    mock_factory.default = m.UNKNOWN
    job = _run(client, ready.id)
    assert job.outcome["outcome"] == ExecutionOutcome.UNKNOWN.value
    _reload(harness, ready)
    assert ready.status == ApplicationStatus.UNCERTAIN.value
    assert harness.item(ready).state == QueueState.NEEDS_REVIEW.value
    assert mock_factory.submit_calls[ready.id] == 1
    # a second explicit action is refused: the attempt is no longer READY
    second = _post(client, ready.id)
    assert second.status_code == 409 and "UNCERTAIN" in second.json()["detail"]
    # and bypassing the route changes nothing: the runner refuses it too
    retried = _wait(get_runner().start(harness.tenant_id, ready.id, "live"))
    assert retried.state == "failed" and "not READY" in retried.error
    assert mock_factory.submit_calls[ready.id] == 1, "no second submit"
    assert len(harness.service.runs_for(ready.id)) == 1


def test_crash_before_the_click_is_retryable_and_the_desktop_does_not_retry(client, harness, ready, mock_factory):  # noqa: F811
    mock_factory.default = m.CRASH_BEFORE_SUBMIT
    job = _run(client, ready.id)
    assert job.outcome["outcome"] == ExecutionOutcome.RETRYABLE_FAILURE.value
    _reload(harness, ready)
    assert ready.status == ApplicationStatus.READY.value
    assert harness.item(ready).state == QueueState.RETRY_WAIT.value
    runs = harness.service.runs_for(ready.id)
    assert len(runs) == 1 and runs[0].submit_invoked is False
    assert mock_factory.submit_calls[ready.id] == 0
    # nothing in the desktop retries on its own: one action, one job, one run
    assert len(get_runner().jobs_for(ready.id)) == 1 and get_runner().active() == []
    time.sleep(0.3)
    assert len(harness.service.runs_for(ready.id)) == 1 and mock_factory.submit_calls[ready.id] == 0


def test_crash_after_the_click_is_unknown(client, harness, ready, mock_factory):  # noqa: F811
    mock_factory.default = m.CRASH_AFTER_SUBMIT
    job = _run(client, ready.id)
    assert job.outcome["outcome"] == ExecutionOutcome.UNKNOWN.value
    _reload(harness, ready)
    assert ready.status == ApplicationStatus.UNCERTAIN.value
    assert harness.service.runs_for(ready.id)[0].submit_invoked is True
    assert mock_factory.submit_calls[ready.id] == 1


# ------------------------------------------------------- 19. exclusivity


def test_an_item_held_by_the_extension_is_not_taken_by_the_desktop(harness, ready, db_session, mock_factory):  # noqa: F811
    item = harness.item(ready)
    assert harness.service.claim_item(item, "ext-worker") is not None
    db_session.commit()
    job = _wait(get_runner().start(harness.tenant_id, ready.id, "live"))
    assert job.state == "failed" and "already being handled" in job.error
    assert harness.service.runs_for(ready.id) == [], "the desktop started no run"
    assert mock_factory.submit_calls[ready.id] == 0
    _reload(harness, ready)
    assert ready.status == ApplicationStatus.READY.value
    assert harness.item(ready).claimed_by == "ext-worker"


# ---------------------------------------------------- 20. tenant isolation


def test_another_tenants_attempt_is_not_found_and_nothing_executes(client, db_session, other_tenant_id, jobs, matches, mock_factory):  # noqa: F811
    foreign = OpportunityFactory(db_session, other_tenant_id, jobs, matches).make(title="Secret Role", company="Other Tenant Inc", fit_score=95)
    attempt = fake_attempt(db_session, other_tenant_id, foreign, status=ApplicationStatus.READY, submitted_days_ago=None)
    db_session.commit()
    response = _post(client, attempt.id)
    assert response.status_code == 404
    assert get_runner().active() == [] and get_runner().jobs_for(attempt.id) == []
    assert mock_factory.submit_calls[attempt.id] == 0


# ------------------------------------------------ 21.-22. the auth contract


def test_the_api_key_path_still_works_and_no_credential_is_refused(client, harness, ready, mock_factory):  # noqa: F811
    anonymous = TestClient(app, follow_redirects=False)
    assert anonymous.post(RUN_URL.format(ready.id), json={"mode": "dry_run"}).status_code == 401
    assert get_runner().jobs_for(ready.id) == []
    keyed = TestClient(app, follow_redirects=False)
    response = keyed.post(RUN_URL.format(ready.id), json={"mode": "live", "confirm": "SUBMIT"}, headers={"X-API-Key": settings.api_key})
    assert response.status_code == 202, response.text
    job = _wait(get_runner().get(response.json()["job_id"]))
    assert job.state == "finished" and job.outcome["outcome"] == ExecutionOutcome.SUBMITTED.value
    assert mock_factory.submit_calls[ready.id] == 1


def test_the_desktop_credential_is_the_cookie_and_the_header_together(client, ready, mock_factory):  # noqa: F811
    # cookie without the desktop header
    assert _post(client, ready.id, headers={}).status_code == 401
    # the header without the cookie
    headerless = TestClient(app, follow_redirects=False)
    assert headerless.post(RUN_URL.format(ready.id), json={"mode": "live", "confirm": "SUBMIT"}, headers=DESKTOP).status_code == 401
    assert get_runner().jobs_for(ready.id) == [] and mock_factory.submit_calls[ready.id] == 0


# ------------------------------------------------- the live confirmation


def test_live_mode_requires_the_typed_confirmation_and_dry_run_is_the_default(client, harness, ready, mock_factory):  # noqa: F811
    refused = _post(client, ready.id, confirm="")
    assert refused.status_code == 400 and refused.json()["detail"] == "type SUBMIT to confirm a real submission"
    assert _post(client, ready.id, confirm="submit").status_code == 400
    assert get_runner().jobs_for(ready.id) == [] and mock_factory.submit_calls[ready.id] == 0
    # no mode at all is a dry run
    response = client.post(RUN_URL.format(ready.id), json={}, headers=DESKTOP)
    assert response.status_code == 202 and response.json()["mode"] == "dry_run"
    _wait(get_runner().get(response.json()["job_id"]))


# ------------------------------- submit_invoked, and never through run_queue


def test_submit_invoked_records_the_live_click_and_a_dry_run_never_claims_one(harness, db_session, ready, monkeypatch):  # noqa: F811
    def never(*args, **kwargs):  # pragma: no cover - the assertion is that it is not reached
        raise AssertionError("a desktop run must never drain the queue")

    monkeypatch.setattr(ExecutionService, "run_queue", never)
    dry_attempt = harness.ready(company="Dry Mock Co", title="Site Reliability Engineer")
    db_session.commit()
    executors = {"live": _ModeAwareMock(dry_run=False), "dry_run": _ModeAwareMock(dry_run=True)}
    monkeypatch.setattr(runner_module, "default_executor_factory", lambda mode: executors[mode])

    live = _wait(get_runner().start(harness.tenant_id, ready.id, "live"))
    assert live.state == "finished" and live.outcome["outcome"] == ExecutionOutcome.SUBMITTED.value
    assert harness.service.runs_for(ready.id)[0].submit_invoked is True
    assert executors["live"].submit_calls[ready.id] == 1

    dry = _wait(get_runner().start(harness.tenant_id, dry_attempt.id, "dry_run"))
    assert dry.state == "finished" and dry.outcome["outcome"] == ExecutionOutcome.DRY_RUN.value
    assert harness.service.runs_for(dry_attempt.id)[0].submit_invoked is False
    assert executors["dry_run"].submit_calls[dry_attempt.id] == 0
    _reload(harness, dry_attempt)
    assert dry_attempt.status == ApplicationStatus.READY.value


# ------------------------------------------------------- the single slot


def test_only_one_desktop_run_at_a_time(client, harness, ready, db_session, monkeypatch):  # noqa: F811
    other = harness.ready(company="Second Co", title="Data Engineer")
    db_session.commit()
    release = threading.Event()

    class _Slow(MockExecutor):
        def prepare(self, package):
            release.wait(20)
            return super().prepare(package)

    monkeypatch.setattr(runner_module, "default_executor_factory", lambda mode: _Slow())
    first = get_runner().start(harness.tenant_id, ready.id, "live")
    try:
        assert _post(client, ready.id).status_code == 409
        assert _post(client, other.id).status_code == 409, "one browser, one attempt at a time"
        with pytest.raises(ConflictError):
            get_runner().start(harness.tenant_id, other.id, "live")
    finally:
        release.set()
    _wait(first)
    assert first.state == "finished" and get_runner().active() == []
    # the slot is free again
    assert _post(client, other.id).status_code == 202
    _wait(get_runner().active_for(other.id) or get_runner().jobs_for(other.id)[0])


def test_an_unknown_job_id_is_not_found(client):
    assert client.get("/desktop/api/runs/does-not-exist").status_code == 404


def test_a_ready_attempt_whose_item_was_parked_is_requeued_by_the_persons_click(client, harness, ready, db_session, mock_factory):  # noqa: F811
    """Pilot finding: a stale preparation parks the queue item (NEEDS_REVIEW),
    the scheduler re-prepares the attempt to READY, and the item stays parked;
    the desktop then failed with "lease held" although nothing held it. The
    person's click re-opens the parked item through the existing retry path —
    the same attempt, one execution, never a drained queue."""
    from app.pipeline.database.models import ApplicationQueueRow

    item = harness.service.item_for(ready)
    harness.service.queue.needs_review(item, "scheduler", "stale preparation: PREPARATION_STALE: inputs changed since preparation")
    db_session.commit()
    assert item.state == "NEEDS_REVIEW" and ready.status == ApplicationStatus.READY.value
    job = _run(client, ready.id)
    assert job.state == "finished" and job.error is None, job.error
    assert job.outcome["outcome"] == "SUBMITTED", job.outcome
    _reload(harness, ready)
    row = db_session.get(ApplicationQueueRow, item.id)
    db_session.refresh(row)
    assert row.state == "SUCCEEDED", "re-queued, claimed and executed exactly once"
    assert mock_factory.submit_calls[ready.id] == 1
    assert db_session.query(ApplicationQueueRow).filter(ApplicationQueueRow.opportunity_id == ready.opportunity_id, ApplicationQueueRow.action == "SUBMIT").count() == 1, "the same item, not a second one"


def test_a_claim_refusal_names_the_real_reason(client, harness, ready, db_session, mock_factory):  # noqa: F811
    """An item held by another worker is reported as such; a parked item that
    cannot be re-opened (CANCELLED) is reported by its state, never as a lease."""
    item = harness.service.item_for(ready)
    harness.service.queue.cancel(item, "person", "cancelled by a person")
    db_session.commit()
    job = _run(client, ready.id)
    assert job.state == "failed" and "CANCELLED" in (job.error or "") and "lease held" not in (job.error or ""), job.error
