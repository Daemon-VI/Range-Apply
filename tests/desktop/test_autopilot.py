"""Autopilot: discover -> match -> prepare on a timer, never a submission."""

import inspect
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import settings
from app.desktop import autopilot as autopilot_module
from app.desktop.__main__ import build_parser
from app.desktop.autopilot import Autopilot
from app.desktop.services import WorkerSupervisor
from app.main import app
from app.security import DASHBOARD_COOKIE, DESKTOP_HEADER, DESKTOP_HEADER_VALUE


def _steps(calls, fail=None):
    def make(name):
        def step(tenant_id, factory):
            calls.append((name, tenant_id))
            if name == fail:
                raise RuntimeError("secret https://employer.example/apply?token=abc")
            return f"{name} done"

        return step

    return {name: make(name) for name in ("discover", "match", "prepare")}


def test_a_cycle_runs_every_step_in_order():
    calls = []
    pilot = Autopilot("t1", interval=1, steps=_steps(calls))
    results = pilot.run_once()
    assert [c[0] for c in calls] == ["discover", "match", "prepare"] and all(c[1] == "t1" for c in calls)
    assert all(r["ok"] for r in results.values()) and pilot.totals["cycles"] == 1


def test_a_failing_step_is_recorded_without_its_message_and_the_rest_still_run():
    calls = []
    pilot = Autopilot("t1", interval=1, steps=_steps(calls, fail="discover"))
    results = pilot.run_once()
    assert [c[0] for c in calls] == ["discover", "match", "prepare"]
    assert results["discover"] == {**results["discover"], "ok": False, "detail": "RuntimeError"}
    assert "token" not in str(pilot.totals) and pilot.totals["errors"] == 1


def test_the_loop_waits_pauses_wakes_and_stops():
    calls = []
    pilot = Autopilot("t1", interval=0.05, start_delay=0.0, steps=_steps(calls))
    thread = threading.Thread(target=pilot.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while pilot.totals["cycles"] < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert pilot.totals["cycles"] >= 2
    pilot.pause()
    time.sleep(0.1)
    paused_at = pilot.totals["cycles"]
    time.sleep(0.2)
    assert pilot.totals["cycles"] == paused_at, "paused: no cycles"
    pilot.run_now()
    deadline = time.time() + 5
    while pilot.totals["cycles"] == paused_at and time.time() < deadline:
        time.sleep(0.01)
    assert pilot.totals["cycles"] == paused_at + 1, "run now works while paused"
    pilot.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_autopilot_never_reaches_execution_or_the_live_switch():
    source = inspect.getsource(autopilot_module)
    for forbidden in ("ExecutionService", "execute(", "submission_mode", "enable_live", "LocalRunner", "get_runner", "PlaywrightExecutor"):
        assert forbidden not in source.split('"""', 2)[2], forbidden


def test_the_desktop_flag_and_the_supervisor():
    args = build_parser().parse_args(["--autopilot", "--autopilot-interval", "10", "--no-window"])
    assert args.autopilot is True and args.autopilot_interval == 10
    assert build_parser().parse_args([]).autopilot is False
    pilot = Autopilot("t1", interval=0.05, start_delay=10, steps=_steps([]))
    supervisor = WorkerSupervisor(lambda: pilot, name="careeros-autopilot")
    assert supervisor.start() and supervisor.target is pilot
    assert supervisor.stop(timeout=5)


def test_the_pause_resume_route_needs_the_desktop_credential_and_a_running_autopilot():
    pilot = Autopilot("t1", interval=60, start_delay=60, steps=_steps([]))
    client = TestClient(app, cookies={DASHBOARD_COOKIE: settings.api_key})
    previous = getattr(app.state, "desktop", None)
    try:
        app.state.desktop = None
        assert client.post("/desktop/api/autopilot", json={"action": "pause"}, headers={DESKTOP_HEADER: DESKTOP_HEADER_VALUE}).status_code == 409
        app.state.desktop = SimpleNamespace(autopilot=SimpleNamespace(target=pilot))
        assert TestClient(app).post("/desktop/api/autopilot", json={"action": "pause"}).status_code == 401
        response = client.post("/desktop/api/autopilot", json={"action": "pause"}, headers={DESKTOP_HEADER: DESKTOP_HEADER_VALUE})
        assert response.status_code == 200 and response.json()["paused"] is True and pilot.paused
        assert client.post("/desktop/api/autopilot", json={"action": "resume"}, headers={DESKTOP_HEADER: DESKTOP_HEADER_VALUE}).json()["paused"] is False
        assert client.post("/desktop/api/autopilot", json={"action": "submit"}, headers={DESKTOP_HEADER: DESKTOP_HEADER_VALUE}).status_code == 422
    finally:
        app.state.desktop = previous
