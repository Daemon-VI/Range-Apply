"""SAFE / LIVE submission mode — a live employer submission is impossible while SAFE.

Server-side only: every refusal below is asserted against the HTTP routes, the
desktop runner, the execution service's pre-submit gate or a fresh process —
never against a hidden button. The ten regressions the safety fix requires:

 1. SAFE + dry run          -> allowed
 2. SAFE + live             -> rejected
 3. LIVE + no SUBMIT        -> rejected
 4. LIVE + wrong phrase     -> rejected
 5. LIVE + exact SUBMIT     -> the existing execution path
 6. Enter cannot bypass the confirmation
 7. changing pages cannot bypass the mode
 8. a direct API request cannot bypass the mode
 9. a desktop request cannot bypass the mode
10. a fresh start is SAFE
"""

import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.application.models import ApplicationStatus
from app.config import PROJECT_ROOT, settings
from app.desktop import runner as runner_module
from app.desktop.runner import LiveSubmissionDisabled, get_runner, reset_runner
from app.execution import submission_mode
from app.execution import worker as worker_module
from app.execution.database.models import ExecutionRunRow
from app.execution.executors.mock import MockExecutor
from app.execution.models import ExecutionOutcome, ExecutionResult, ExecutorKind
from app.main import app
from app.security import DASHBOARD_COOKIE, DESKTOP_HEADER, DESKTOP_HEADER_VALUE
from tests.execution.conftest import harness  # noqa: F401
from tests.scheduler.conftest import (  # noqa: F401
    answered_bank,
    db_session,
    evidence,
    jobs,
    matches,
    opportunities,
    other_tenant_id,
    scheduler,
    tenant_id,
)

COOKIE = {DASHBOARD_COOKIE: settings.api_key}
DESKTOP = {DESKTOP_HEADER: DESKTOP_HEADER_VALUE}
RUN_URL = "/desktop/api/attempts/{}/run"
MODE_URL = "/desktop/api/submission-mode"
SAFE_DETAIL = "SAFE / DRY RUN"


# ------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def _fresh_runner():
    reset_runner()
    yield
    reset_runner()


@pytest.fixture
def client(tenant_id):  # noqa: F811
    app.dependency_overrides[get_tenant_id] = lambda: tenant_id
    try:
        yield TestClient(app, follow_redirects=False, cookies=COOKIE)
    finally:
        app.dependency_overrides.pop(get_tenant_id, None)


@pytest.fixture
def ready(harness, db_session):  # noqa: F811
    attempt = harness.ready(company="Safe Mode Corp", title="Platform Engineer", fit_score=88)
    db_session.commit()
    return attempt


class _ModeAwareMock(MockExecutor):
    """The real executor's contract: a dry run returns before the gate and never presses submit."""

    def __init__(self, dry_run: bool, **kwargs):
        super().__init__(**kwargs)
        self.dry_run = dry_run

    def execute(self, package, form, answers, gate=None) -> ExecutionResult:
        if self.dry_run:
            return ExecutionResult(outcome=ExecutionOutcome.DRY_RUN, message="dry run: submit deliberately not pressed")
        return super().execute(package, form, answers, gate=gate)


@pytest.fixture
def executors(monkeypatch):
    built: dict[str, _ModeAwareMock] = {}

    def factory(mode):
        built[mode] = _ModeAwareMock(dry_run=(mode != "live"))
        return built[mode]

    monkeypatch.setattr(runner_module, "default_executor_factory", factory)
    return built


def _wait(job, timeout: float = 60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not job.running:
            return job
        time.sleep(0.02)
    raise AssertionError(f"desktop run {job.id} never finished: {job.as_dict()}")


def _runs(harness, attempt):  # noqa: F811
    harness.session.rollback()
    return harness.session.query(ExecutionRunRow).filter(ExecutionRunRow.application_id == attempt.id).all()


def _enable(client):
    response = client.post(MODE_URL, json={"mode": "live"}, headers=DESKTOP)
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "LIVE" and response.json()["live_submission_enabled"] is True


def _assert_nothing_started(harness, ready, executors):  # noqa: F811
    assert get_runner().active() == [] and get_runner().jobs_for(ready.id) == []
    assert "live" not in executors, "no live executor (no browser) may even be built"
    assert _runs(harness, ready) == []
    harness.session.refresh(ready)
    assert ready.status == ApplicationStatus.READY.value


# ------------------------------------------------------ 1. SAFE + dry run


def test_1_safe_mode_allows_a_dry_run(client, harness, ready, executors, tenant_id):  # noqa: F811
    assert submission_mode.get_state(tenant_id).mode == submission_mode.SAFE
    response = client.post(RUN_URL.format(ready.id), json={"mode": "dry_run"}, headers=DESKTOP)
    assert response.status_code == 202, response.text
    job = _wait(get_runner().get(response.json()["job_id"]))
    assert job.state == "finished" and job.outcome["outcome"] == ExecutionOutcome.DRY_RUN.value
    assert sum(executors["dry_run"].submit_calls.values()) == 0
    assert [r.submit_invoked for r in _runs(harness, ready)] == [False]


# --------------------------------------------------------- 2. SAFE + live


@pytest.mark.parametrize("confirm", ["SUBMIT", "", "submit"])
def test_2_safe_mode_rejects_a_live_run_even_with_the_typed_phrase(client, harness, ready, executors, confirm):  # noqa: F811
    response = client.post(RUN_URL.format(ready.id), json={"mode": "live", "confirm": confirm}, headers=DESKTOP)
    assert response.status_code == 403 and SAFE_DETAIL in response.json()["detail"]
    _assert_nothing_started(harness, ready, executors)


# ------------------------------------------------- 3 / 4. LIVE, no or wrong phrase


def test_3_live_mode_without_the_typed_phrase_is_rejected(client, harness, ready, executors):  # noqa: F811
    _enable(client)
    for body in ({"mode": "live"}, {"mode": "live", "confirm": ""}):
        response = client.post(RUN_URL.format(ready.id), json=body, headers=DESKTOP)
        assert response.status_code == 400 and "SUBMIT" in response.json()["detail"]
    _assert_nothing_started(harness, ready, executors)


@pytest.mark.parametrize("confirm", ["submit", "Submit", " SUBMIT", "SUBMIT ", "SUBMIT\n", "YES", "SUBMITT", "LIVE"])
def test_4_live_mode_with_an_incorrect_phrase_is_rejected(client, harness, ready, executors, confirm):  # noqa: F811
    _enable(client)
    response = client.post(RUN_URL.format(ready.id), json={"mode": "live", "confirm": confirm}, headers=DESKTOP)
    assert response.status_code == 400
    _assert_nothing_started(harness, ready, executors)


# ------------------------------------------------ 5. LIVE + exact SUBMIT


def test_5_live_mode_with_exact_submit_enters_the_existing_execution_path(client, harness, ready, executors):  # noqa: F811
    _enable(client)
    response = client.post(RUN_URL.format(ready.id), json={"mode": "live", "confirm": "SUBMIT"}, headers=DESKTOP)
    assert response.status_code == 202, response.text
    job = _wait(get_runner().get(response.json()["job_id"]))
    assert job.state == "finished" and job.mode == "live"
    # ExecutionService ran: preconditions, pre-submit gate, one click, verification.
    assert job.outcome["outcome"] == ExecutionOutcome.SUBMITTED.value
    assert executors["live"].submit_calls[ready.id] == 1
    assert [r.submit_invoked for r in _runs(harness, ready)] == [True]


def test_switching_back_to_safe_mid_run_stops_the_click_at_the_gate(client, harness, ready, monkeypatch, tenant_id):  # noqa: F811
    class _SwitchesToSafe(_ModeAwareMock):
        def execute(self, package, form, answers, gate=None):
            submission_mode.set_safe(tenant_id, actor="person")  # the person clicks "Return to SAFE" while the form fills
            return super().execute(package, form, answers, gate=gate)

    executor = _SwitchesToSafe(dry_run=False)
    monkeypatch.setattr(runner_module, "default_executor_factory", lambda mode: executor)
    _enable(client)
    response = client.post(RUN_URL.format(ready.id), json={"mode": "live", "confirm": "SUBMIT"}, headers=DESKTOP)
    job = _wait(get_runner().get(response.json()["job_id"]))
    assert sum(executor.submit_calls.values()) == 0, "the gate refused: nothing was pressed"
    assert job.outcome["outcome"] == ExecutionOutcome.NEEDS_REVIEW.value
    runs = _runs(harness, ready)
    assert len(runs) == 1 and "SAFE_MODE" in (runs[0].error_message or "")
    harness.session.refresh(ready)
    assert ready.status not in (ApplicationStatus.SUBMITTED.value, ApplicationStatus.VERIFIED.value)


# ------------------------------------------------ 6. Enter cannot bypass


def test_6_enter_key_cannot_bypass_the_confirmation(client, harness, ready, executors):  # noqa: F811
    _enable(client)
    html = client.get(f"/desktop/applications/{ready.id}/confirm?mode=live").text
    # The form never submits natively (Enter does nothing) and the button starts disabled
    # and only posts when the exact phrase is in the input.
    assert 'id="run-form" onsubmit="return false"' in html and "<form id=\"run-form\" onsubmit=\"return false\" class" in html
    assert 'id="confirm-live" class="danger-solid lg" disabled' in html
    assert 'data-require-input="confirm-phrase" data-require-value="SUBMIT"' in html
    assert 'action="/desktop/api/attempts' not in html
    # What a native Enter submission would send (form-encoded, no JavaScript): refused.
    native = client.post(RUN_URL.format(ready.id), data={"mode": "live", "confirm": ""}, headers=DESKTOP)
    assert native.status_code == 422
    native_typed = client.post(RUN_URL.format(ready.id), data={"mode": "live", "confirm": "SUBMIT"}, headers=DESKTOP)
    assert native_typed.status_code == 422
    _assert_nothing_started(harness, ready, executors)


# ------------------------------------------------ 7. changing pages


def test_7_changing_pages_cannot_bypass_the_mode(client, harness, ready, executors, tenant_id):  # noqa: F811
    for path in ("/desktop/", "/desktop/applications", f"/desktop/applications/{ready.id}", f"/desktop/applications/{ready.id}/confirm?mode=live",
                 f"/desktop/applications/{ready.id}/confirm?mode=live&live=1", "/desktop/system", f"{MODE_URL}?mode=live"):
        assert client.get(path).status_code == 200, path
    assert submission_mode.get_state(tenant_id).mode == submission_mode.SAFE
    assert client.get(MODE_URL).json()["mode"] == "SAFE"

    confirm = client.get(f"/desktop/applications/{ready.id}/confirm?mode=live").text
    assert 'id="live-disabled"' in confirm and 'id="confirm-live"' not in confirm and 'name="confirm"' not in confirm
    assert 'class="safety-banner safe"' in confirm and "Enable live submission" in confirm
    review = client.get(f"/desktop/applications/{ready.id}").text
    assert "SAFE / DRY RUN" in review and "the server refuses a real submission" in review

    assert client.post(RUN_URL.format(ready.id), json={"mode": "live", "confirm": "SUBMIT"}, headers=DESKTOP).status_code == 403

    # LIVE -> pages -> back to SAFE: the confirmation disappears again and the route refuses again.
    _enable(client)
    assert 'id="confirm-live"' in client.get(f"/desktop/applications/{ready.id}/confirm?mode=live").text
    assert client.post(MODE_URL, json={"mode": "safe"}, headers=DESKTOP).json()["mode"] == "SAFE"
    assert 'id="confirm-live"' not in client.get(f"/desktop/applications/{ready.id}/confirm?mode=live").text
    assert client.post(RUN_URL.format(ready.id), json={"mode": "live", "confirm": "SUBMIT"}, headers=DESKTOP).status_code == 403
    _assert_nothing_started(harness, ready, executors)


# ------------------------------------------------ 8. direct API requests


def test_8_direct_api_requests_cannot_bypass_the_mode(harness, ready, executors, tenant_id):  # noqa: F811
    app.dependency_overrides[get_tenant_id] = lambda: tenant_id
    try:
        keyed = TestClient(app, follow_redirects=False)
        key = {"X-API-Key": settings.api_key}
        response = keyed.post(RUN_URL.format(ready.id), json={"mode": "live", "confirm": "SUBMIT"}, headers=key)
        assert response.status_code == 403
        # The switch itself needs a credential: no key, or a cookie without the desktop header, is refused.
        assert keyed.post(MODE_URL, json={"mode": "live"}).status_code == 401
        assert TestClient(app, cookies=COOKIE).post(MODE_URL, json={"mode": "live"}).status_code == 401
        assert TestClient(app, cookies=COOKIE).get(MODE_URL, params={"mode": "live"}).json()["mode"] == "SAFE"
        assert keyed.put(MODE_URL, json={"mode": "live"}, headers=key).status_code == 405
        assert keyed.post(MODE_URL, json={"mode": "LIVE"}, headers=key).status_code == 422
        assert keyed.post(MODE_URL, json={"mode": True}, headers=key).status_code == 422
        # The extension reads the mode, and its pre-submit gate refuses the click.
        status = keyed.get("/api/v1/execution/extension/status", headers=key)
        assert status.status_code == 200 and status.json()["live_submission_enabled"] is False
    finally:
        app.dependency_overrides.pop(get_tenant_id, None)
    assert submission_mode.get_state(tenant_id).mode == submission_mode.SAFE

    # In-process callers get no way around it either.
    with pytest.raises(LiveSubmissionDisabled):
        get_runner().start(tenant_id, ready.id, "live")
    _assert_nothing_started(harness, ready, executors)


def test_8_the_pre_submit_gate_refuses_every_executor_while_safe(harness, ready, tenant_id):  # noqa: F811
    assert submission_mode.SAFE_MODE_FAILURE in harness.service._pre_submit_gate(ready.id)()

    # The extension's gate call, with the service the extension routes build: claimed, started, then asks to click.
    from app.execution.service import ExecutionService

    service = ExecutionService(harness.session, tenant_id, actor="api")
    item = service.item_for(ready)
    claimed = service.claim_item(item, "ext-safe")
    run, _package, _outcome = service.start(claimed, "ext-safe", ExecutorKind.BROWSER_EXTENSION)
    assert run is not None
    verdict = service.gate(claimed, "ext-safe")
    assert verdict["ok"] is False and submission_mode.SAFE_MODE_FAILURE in verdict["failures"]
    harness.session.refresh(run)
    assert run.submit_invoked is False


def test_8_the_mock_queue_runner_never_clicks_while_safe(harness, ready):  # noqa: F811
    harness.service.run_queue("queue-safe", 5, ExecutorKind.MOCK)
    assert sum(harness.mock.submit_calls.values()) == 0
    harness.session.refresh(ready)
    assert ready.status not in (ApplicationStatus.SUBMITTED.value, ApplicationStatus.VERIFIED.value)
    runs = _runs(harness, ready)
    assert runs and all("SAFE_MODE" in (r.error_message or "") for r in runs)


def test_live_is_per_tenant(client, harness, ready, executors, tenant_id, other_tenant_id):  # noqa: F811
    submission_mode.enable_live(other_tenant_id, actor="other")
    assert not submission_mode.is_live_enabled(tenant_id)
    assert client.post(RUN_URL.format(ready.id), json={"mode": "live", "confirm": "SUBMIT"}, headers=DESKTOP).status_code == 403
    _assert_nothing_started(harness, ready, executors)


# ------------------------------------------------ 9. desktop requests


def test_9_desktop_requests_cannot_bypass_the_mode(client, harness, ready, executors):  # noqa: F811
    # The desktop credential (cookie + header), with every extra a crafted request might try.
    crafted = {**DESKTOP, "X-CareerOS-Mode": "live", "Origin": "http://testserver"}
    body = {"mode": "live", "confirm": "SUBMIT", "live": True, "force": True, "submission_mode": "LIVE"}
    response = client.post(RUN_URL.format(ready.id), json=body, headers=crafted)
    assert response.status_code == 403
    response = client.post(RUN_URL.format(ready.id) + "?mode=live&live=true", json={"mode": "live", "confirm": "SUBMIT"}, headers=DESKTOP)
    assert response.status_code == 403
    # A cross-site page cannot flip the switch for the person.
    assert client.post(MODE_URL, json={"mode": "live"}, headers={**DESKTOP, "Origin": "http://evil.example"}).status_code in (401, 403)
    assert client.get(MODE_URL).json()["mode"] == "SAFE"
    _assert_nothing_started(harness, ready, executors)


# ------------------------------------------------ 10. fresh start


def test_10_a_fresh_process_starts_safe_whatever_the_environment():
    env = {**os.environ, "PLAYWRIGHT_DRY_RUN": "false", "PYTHONIOENCODING": "utf-8"}
    code = (
        "import app.main\n"
        "from app.config import settings\n"
        "from app.execution import submission_mode as s\n"
        "print('MODE', s.get_state(settings.default_tenant_id).mode, s.is_live_enabled(settings.default_tenant_id), s.is_live_enabled('any-tenant'))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "MODE SAFE False False" in result.stdout


def test_10_reset_returns_every_tenant_to_safe(tenant_id, other_tenant_id):  # noqa: F811
    submission_mode.enable_live(tenant_id, actor="test")
    submission_mode.enable_live(other_tenant_id, actor="test")
    submission_mode.reset()
    assert not submission_mode.is_live_enabled(tenant_id) and not submission_mode.is_live_enabled(other_tenant_id)
    assert not submission_mode.is_live_enabled("") and not submission_mode.is_live_enabled(None)


class _DummyWorker:
    def __init__(self, tenant, worker_id, dry_run=None, limit=1, headless=None):
        self.dry_run = dry_run

    def run(self, once=False):
        return {}

    def stop(self, *args):
        pass


def test_the_standalone_worker_needs_an_explicit_live_flag(monkeypatch):
    monkeypatch.setattr(worker_module, "LocalWorker", _DummyWorker)
    monkeypatch.setattr(worker_module, "configure_logging", lambda level: None)
    monkeypatch.setattr(worker_module.signal, "signal", lambda *args: None)

    monkeypatch.setattr(settings, "playwright_dry_run", False)
    with pytest.raises(SystemExit):
        worker_module.main(["--tenant", "worker-safe", "--once"])
    assert not submission_mode.is_live_enabled("worker-safe")

    monkeypatch.setattr(settings, "playwright_dry_run", True)
    assert worker_module.main(["--tenant", "worker-safe", "--once"]) == 0
    assert not submission_mode.is_live_enabled("worker-safe")

    assert worker_module.main(["--tenant", "worker-live", "--once", "--live"]) == 0
    assert submission_mode.is_live_enabled("worker-live")


def test_banner_shows_the_switch_on_every_desktop_page(client, ready):
    html = client.get("/desktop/").text
    assert 'data-mode="SAFE"' in html and 'data-body=\'{"mode": "live"}\'' in html and "Enable live submission" in html
    _enable(client)
    html = client.get("/desktop/").text
    assert 'class="safety-banner live"' in html and 'data-mode="LIVE"' in html and "Return to SAFE" in html
