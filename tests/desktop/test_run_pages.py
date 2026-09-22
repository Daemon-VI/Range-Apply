"""Increment 4: the run page and its polling partial, through TestClient.

The pages read the desktop run supervisor (``app/desktop/runner.py``, built
alongside this UI): a job is registered directly in the supervisor and the
execution run it points at is written to the database, so the pages are
exercised without ever opening a browser. The module is imported through
``importorskip`` so this file skips cleanly until the supervisor lands.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.config import settings
from app.execution.database.models import ExecutionRunRow
from app.main import app
from app.security import DASHBOARD_COOKIE
from tests.execution.conftest import harness  # noqa: F401
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

runner_module = pytest.importorskip("app.desktop.runner")

COOKIE = {DASHBOARD_COOKIE: settings.api_key}


@pytest.fixture
def client(tenant_id):  # noqa: F811
    app.dependency_overrides[get_tenant_id] = lambda: tenant_id
    try:
        yield TestClient(app, follow_redirects=False, cookies=COOKIE)
    finally:
        app.dependency_overrides.pop(get_tenant_id, None)


@pytest.fixture
def ready(harness):  # noqa: F811
    return harness.ready(company="Desktop Corp", title="Platform Engineer", fit_score=88)


def _registry(runner):
    for name in ("_jobs", "jobs", "_registry", "_by_id"):
        store = getattr(runner, name, None)
        if isinstance(store, dict):
            return store
    pytest.skip("the run supervisor exposes no job registry this test can seed")


@pytest.fixture
def register():
    """Put a RunJob straight into the supervisor and take it out again."""
    runner = runner_module.get_runner()
    store = _registry(runner)
    added: list[str] = []

    def _add(attempt, **overrides):
        job_class = getattr(runner_module, "RunJob", None)
        if job_class is None:
            pytest.skip("app.desktop.runner exposes no RunJob class")
        now = datetime.now()
        fields = {
            "id": uuid.uuid4().hex,
            "tenant_id": attempt.tenant_id,
            "application_id": attempt.id,
            "mode": "dry_run",
            "worker_id": "desktop-1",
            "state": "running",
            "started_at": now - timedelta(seconds=5),
            "finished_at": None,
            "outcome": None,
            "run_id": None,
            "error": None,
        }
        fields.update(overrides)
        try:
            job = job_class(**fields)
        except TypeError as exc:  # the contract moved: say so instead of failing obscurely
            pytest.skip(f"RunJob does not accept the Increment 4 fields: {exc}")
        store[job.id] = job
        added.append(job.id)
        return job

    yield _add
    for job_id in added:
        store.pop(job_id, None)


def _run(db, attempt, **fields) -> ExecutionRunRow:
    now = datetime.now()
    row = ExecutionRunRow(
        tenant_id=attempt.tenant_id, application_id=attempt.id, preparation_id=attempt.preparation_id,
        executor_kind="PLAYWRIGHT_LOCAL", executor_version="test", worker_id="desktop-1",
        idempotency_key=uuid.uuid4().hex, run_number=1, status="DRY_RUN",
        started_at=now - timedelta(seconds=20), finished_at=now,
    )
    for key, value in fields.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()
    return row


# ------------------------------------------------------------------ running


def test_run_page_while_running_tells_you_to_watch_and_polls_the_partial(client, ready, register):
    job = register(ready, mode="live")
    page = client.get(f"/desktop/applications/{ready.id}/run/{job.id}")
    assert page.status_code == 200
    html = page.text
    assert "Running — watch the browser window. Do not close it." in html
    assert "LIVE — submit will be pressed once." in html and "desktop-1" in html
    assert f'hx-get="/desktop/partials/run/{job.id}"' in html and 'hx-trigger="every 2s"' in html and 'hx-swap="outerHTML"' in html
    assert "Back to review" in html and "Attention center" in html
    assert settings.api_key not in html and "X-API-Key" not in html
    partial = client.get(f"/desktop/partials/run/{job.id}")
    assert partial.status_code == 200 and "Running — watch the browser window" in partial.text


def test_review_and_confirm_hide_the_buttons_while_a_run_is_in_progress(client, ready, register):
    job = register(ready)
    review = client.get(f"/desktop/applications/{ready.id}").text
    assert "A run is in progress" in review
    assert f'href="/desktop/applications/{ready.id}/run/{job.id}"' in review
    assert 'confirm?mode=live"' not in review and 'confirm?mode=dry_run"' not in review
    confirm = client.get(f"/desktop/applications/{ready.id}/confirm?mode=live").text
    assert "A run for this application is already in progress." in confirm
    assert "data-require-value" not in confirm


# ------------------------------------------------------------------ results


def test_dry_run_result_reports_the_fields_and_stops_polling(client, ready, register, db_session):  # noqa: F811
    run = _run(db_session, ready, status="DRY_RUN", outcome="DRY_RUN", submit_invoked=False, diagnostics={"fields_filled": 7, "fields_skipped": 2})
    job = register(ready, state="finished", finished_at=datetime.now(), run_id=run.id, outcome={"outcome": "DRY_RUN", "run_id": run.id, "attempt_status": "READY", "queue_state": "DONE"})
    html = client.get(f"/desktop/applications/{ready.id}/run/{job.id}").text
    assert "DRY RUN COMPLETE" in html
    assert "7 field(s) filled, 2 skipped, submit not pressed." in html
    assert "Submit pressed:" in html and ">no<" in html
    assert 'hx-trigger="every 2s"' not in html
    assert "success" not in html.lower() or "VERIFIED" in html


def test_unknown_outcome_is_never_resubmitted_and_asks_you_to_confirm(client, ready, register, db_session):  # noqa: F811
    run = _run(db_session, ready, status="VERIFICATION_FAILED", outcome="UNKNOWN", submit_invoked=True, verification_status="UNKNOWN", verification_method="page_signature", verification_detail="no confirmation could be read")
    job = register(ready, mode="live", state="finished", finished_at=datetime.now(), run_id=run.id, outcome={"outcome": "UNKNOWN", "run_id": run.id, "attempt_status": "UNCERTAIN", "queue_state": "DONE"})
    html = client.get(f"/desktop/applications/{ready.id}/run/{job.id}").text
    assert "UNKNOWN / UNCERTAIN" in html
    assert "CareerOS will NOT resubmit this application automatically." in html
    assert f'data-url="/api/v1/execution/runs/{run.id}/confirm"' in html
    assert '{"submitted": true' in html and '{"submitted": false' in html
    assert "It was submitted" in html and "It was not submitted" in html
    assert "SUBMITTED / VERIFIED" not in html
    assert "no confirmation could be read" in html


def test_blocked_run_explains_the_handoff_and_links_to_the_page(client, ready, register, db_session):  # noqa: F811
    run = _run(
        db_session, ready, status="HANDOFF", outcome="HANDOFF", submit_invoked=False, handoff_reason="CAPTCHA_REQUIRED",
        application_url="https://employer.test/apply/42",
        handoff={"reason": "CAPTCHA_REQUIRED", "message": "a visible challenge was shown before the form", "remaining_steps": ["complete the challenge", "submit the form"]},
    )
    job = register(ready, mode="live", state="finished", finished_at=datetime.now(), run_id=run.id, outcome={"outcome": "HANDOFF", "run_id": run.id, "attempt_status": "BLOCKED", "queue_state": "PARKED"})
    html = client.get(f"/desktop/applications/{ready.id}/run/{job.id}").text
    assert "BLOCKED" in html
    assert "CareerOS reached a bot-protection challenge on the employer" in html  # the sentence is HTML-escaped
    assert "only a person may complete it" in html
    assert "a visible challenge was shown before the form" in html
    assert "complete the challenge" in html
    assert 'href="https://employer.test/apply/42"' in html and "Open in browser" in html


def test_permanent_failure_is_not_retried_automatically(client, ready, register, db_session):  # noqa: F811
    run = _run(db_session, ready, status="FAILED_PERMANENT", outcome="PERMANENT_FAILURE", error_class="NAVIGATION", error_message="the posting returned 404")
    job = register(ready, state="finished", finished_at=datetime.now(), run_id=run.id, outcome={"outcome": "PERMANENT_FAILURE", "run_id": run.id, "attempt_status": "FAILED", "queue_state": "FAILED"})
    html = client.get(f"/desktop/applications/{ready.id}/run/{job.id}").text
    assert "PERMANENT FAILURE" in html and "Not retried automatically" in html
    assert "NAVIGATION" in html and "the posting returned 404" in html


def test_a_job_that_never_started_shows_the_supervisor_error(client, ready, register):
    job = register(ready, mode="live", state="failed", finished_at=datetime.now(), error="already being handled by another worker/executor")
    html = client.get(f"/desktop/applications/{ready.id}/run/{job.id}").text
    assert "RUN DID NOT START" in html
    assert "already being handled by another worker/executor" in html
    assert "no execution run was created" in html


# -------------------------------------------------------------- isolation


def test_a_foreign_or_unknown_job_is_not_found(client, ready, register, db_session, other_tenant_id, jobs, matches):  # noqa: F811
    foreign_co = OpportunityFactory(db_session, other_tenant_id, jobs, matches).make(title="Secret Role", company="Other Tenant Inc", fit_score=95)
    foreign = fake_attempt(db_session, other_tenant_id, foreign_co)
    db_session.commit()
    foreign_job = register(foreign)
    assert client.get(f"/desktop/applications/{foreign.id}/run/{foreign_job.id}").status_code == 404
    assert client.get(f"/desktop/partials/run/{foreign_job.id}").status_code == 404
    assert client.get(f"/desktop/applications/{ready.id}/run/{uuid.uuid4().hex}").status_code == 404
    # a job that belongs to a different attempt of this tenant is not reachable from this one
    mine = register(ready)
    assert client.get(f"/desktop/applications/{foreign.id}/run/{mine.id}").status_code == 404


def test_needs_user_input_result_has_its_own_headline_and_names_the_reason(client, ready, register, db_session):  # noqa: F811
    """Pilot finding: a NEEDS_USER_INPUT dry run was headlined NEEDS REVIEW,
    which hides the one thing the person must do (answer the questions)."""
    run = _run(db_session, ready, status="NEEDS_USER_INPUT", outcome="NEEDS_USER_INPUT", submit_invoked=False, error_message="3 required field(s) need the candidate")
    ready.status = "NEEDS_USER_INPUT"
    ready.status_reason = "3 required field(s) need the candidate"
    db_session.commit()
    job = register(ready, state="finished", finished_at=datetime.now(), run_id=run.id, outcome={"outcome": "NEEDS_USER_INPUT", "run_id": run.id, "attempt_status": "NEEDS_USER_INPUT", "queue_state": "BLOCKED"})
    html = client.get(f"/desktop/applications/{ready.id}/run/{job.id}").text
    assert "NEEDS USER INPUT" in html and "3 required field(s) need the candidate" in html
    assert "nothing was invented" in html and "Answer them on the review page" in html
    assert ">NEEDS REVIEW<" not in html
    assert "Submit pressed:" in html and ">no<" in html


def test_handoff_that_stopped_before_the_form_never_reads_as_submit_pressed(client, ready, register, db_session):  # noqa: F811
    """Pilot finding: ``submit_invoked`` is recorded before the executor may
    press submit (so a crash can never read as "not submitted"), so a CAPTCHA
    handoff on the landing page carried the flag and the page said
    "Submit pressed: yes" although nothing was typed, let alone pressed."""
    run = _run(db_session, ready, status="HANDOFF", outcome="HANDOFF", submit_invoked=True, handoff_reason="CAPTCHA_REQUIRED", handoff={"reason": "CAPTCHA_REQUIRED", "message": "a visible challenge was shown before the form", "stopped_at": "before_form", "remaining_steps": []})
    job = register(ready, state="finished", finished_at=datetime.now(), run_id=run.id, outcome={"outcome": "HANDOFF", "run_id": run.id, "attempt_status": "BLOCKED", "queue_state": "BLOCKED"})
    html = client.get(f"/desktop/applications/{ready.id}/run/{job.id}").text
    assert "Submit pressed:" in html and ">no<" in html and 'badge-orange">yes' not in html
    # A handoff after the click (a challenge shown once submit was pressed) keeps the truthful "yes".
    after = _run(db_session, ready, status="HANDOFF", outcome="HANDOFF", submit_invoked=True, handoff_reason="CAPTCHA_REQUIRED", handoff={"reason": "CAPTCHA_REQUIRED", "message": "challenge after the click", "stopped_at": "after_submit", "remaining_steps": []})
    job2 = register(ready, state="finished", finished_at=datetime.now(), run_id=after.id, outcome={"outcome": "HANDOFF", "run_id": after.id, "attempt_status": "BLOCKED", "queue_state": "BLOCKED"})
    html2 = client.get(f"/desktop/applications/{ready.id}/run/{job2.id}").text
    assert 'badge-orange">yes' in html2
