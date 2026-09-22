"""Desktop shell, Increment 1: port choice, readiness, server lifecycle,
login tokens, worker supervision and shutdown — all without a window."""

import socket
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.desktop.launcher import (
    LOOPBACK,
    DesktopApp,
    LoginTokens,
    ServerHandle,
    check_readiness,
    find_free_port,
)
from app.desktop.services import WorkerSupervisor
from app.main import app
from app.security import DASHBOARD_COOKIE

# ------------------------------------------------------------------ port


def test_free_port_prefers_the_requested_one_and_falls_back_when_busy():
    holder = socket.socket()
    holder.bind((LOOPBACK, 0))
    busy = holder.getsockname()[1]
    try:
        chosen = find_free_port(preferred=busy)
        assert chosen != busy and 1024 < chosen < 65536
    finally:
        holder.close()
    free = find_free_port(preferred=None)
    assert find_free_port(preferred=free) == free


def test_server_handle_refuses_anything_but_loopback():
    with pytest.raises(ValueError):
        ServerHandle(app, host="0.0.0.0")


# ------------------------------------------------------------- readiness


def test_readiness_reports_migrated_test_database_and_configured_key():
    report = check_readiness()
    assert report.ready and report.database_reachable and report.database_migrated
    assert report.missing_tables == [] and report.api_key_configured and "sqlite" in report.database_url
    assert report.as_dict()["database"]["migrated"] is True


def test_readiness_refuses_without_api_key_outside_development(monkeypatch):
    monkeypatch.setattr(settings, "api_key", None)
    monkeypatch.setattr(settings, "app_env", "production")
    report = check_readiness()
    assert not report.ready and any("API_KEY" in p for p in report.problems)
    monkeypatch.setattr(settings, "app_env", "development")
    assert check_readiness().ready, "development tolerates an unset key, like the server does"


def test_readiness_reports_a_missing_schema_and_an_unreachable_database(monkeypatch):
    import app.desktop.launcher as launcher

    monkeypatch.setattr(launcher, "missing_tables", lambda: ["applications", "signals"])
    report = check_readiness()
    assert not report.ready and report.database_reachable and not report.database_migrated
    assert report.missing_tables == ["applications", "signals"]
    assert any("alembic upgrade head" in p for p in report.problems)

    def boom():
        raise OSError("disk gone")

    monkeypatch.setattr(launcher, "missing_tables", boom)
    report = check_readiness()
    assert not report.ready and not report.database_reachable
    assert any("not reachable" in p for p in report.problems)


# --------------------------------------------------------------- tokens


def test_login_tokens_are_single_use():
    tokens = LoginTokens()
    token = tokens.mint()
    assert len(tokens) == 1 and not tokens.consume("nope") and not tokens.consume(None)
    assert tokens.consume(token) and len(tokens) == 0
    assert not tokens.consume(token), "a token works exactly once"


def test_login_route_consumes_the_token_sets_the_cookie_and_status_is_guarded():
    shell = DesktopApp(app, open_window=lambda url: None)
    app.state.desktop = shell
    try:
        client = TestClient(app, follow_redirects=False)
        assert client.get("/desktop/login?token=bogus").status_code == 401
        url = shell.login_url()
        assert "token=" in url and settings.api_key not in url, "the API key never travels in a URL"
        path = url.split(shell.server.url, 1)[1]
        response = client.get(path)
        assert response.status_code == 303 and response.headers["location"] == "/dashboard/"
        assert response.cookies.get(DASHBOARD_COOKIE) == settings.api_key
        assert client.get(path).status_code == 401, "single use"
        token = shell.tokens.mint()
        evil = client.get(f"/desktop/login?token={token}&next=//evil.example/")
        assert evil.headers["location"] == "/dashboard/", "no open redirect"
        anonymous = TestClient(app, follow_redirects=False)
        assert anonymous.get("/desktop/status").status_code == 401
        status = client.get("/desktop/status").json()
        assert set(status) == {"server", "readiness", "health", "worker", "notifications", "autopilot", "window"}
        assert status["worker"] == {"state": "disabled"} and status["notifications"] == {"state": "disabled"} and status["autopilot"] == {"state": "disabled"}
        assert settings.api_key not in str(status)
    finally:
        app.state.desktop = None


# ------------------------------------------------------------ supervisor


class FakeWorker:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self._stop = threading.Event()
        self.totals = {"claimed": 0}
        self.executor = type("Exec", (), {"dry_run": True})()

    def run(self):
        if self.fail:
            raise RuntimeError("browser exploded")
        while not self._stop.is_set():
            self.totals["claimed"] += 1
            time.sleep(0.02)
        return dict(self.totals)

    def stop(self, *_):
        self._stop.set()


def test_supervisor_starts_once_reports_and_stops_the_worker():
    worker = FakeWorker()
    supervisor = WorkerSupervisor(lambda: worker, name="test-worker")
    assert supervisor.status()["state"] == "idle"
    assert supervisor.start() and supervisor.running
    assert not supervisor.start(), "a second start is a no-op"
    time.sleep(0.1)
    status = supervisor.status()
    assert status["state"] == "running" and status["dry_run"] is True and status["totals"]["claimed"] >= 1
    assert supervisor.stop(timeout=5) and not supervisor.running
    assert supervisor.status()["state"] == "stopped"
    assert supervisor.stop() is True, "stopping twice is harmless"


def test_supervisor_reports_a_worker_that_fails_or_cannot_be_built():
    supervisor = WorkerSupervisor(lambda: FakeWorker(fail=True))
    assert supervisor.start()
    time.sleep(0.2)
    status = supervisor.status()
    assert status["state"] == "failed" and "browser exploded" in status["error"]

    def bad_factory():
        raise ImportError("playwright is not installed")

    broken = WorkerSupervisor(bad_factory)
    assert not broken.start()
    assert broken.status()["state"] == "failed" and "playwright" in broken.status()["error"]


# ------------------------------------------------------------ lifecycle


def test_desktop_app_starts_serves_on_loopback_and_stops_everything_in_order():
    events: list[str] = []

    class OrderedWorker(FakeWorker):
        def stop(self, *_):
            events.append("worker-stop")
            super().stop()

    supervisor = WorkerSupervisor(lambda: OrderedWorker())
    opened: list[str] = []
    shell: DesktopApp

    def fake_window(url: str) -> None:
        opened.append(url)
        health = httpx.get(f"{shell.server.url}/health", timeout=5).json()
        assert health["status"] == "ok"
        cookies = {DASHBOARD_COOKIE: settings.api_key}
        status = httpx.get(f"{shell.server.url}/desktop/status", cookies=cookies, timeout=5).json()
        assert status["server"]["running"] and status["worker"]["state"] == "running"
        assert status["readiness"]["ready"]
        events.append("window-closed")

    shell = DesktopApp(app, worker_supervisor=supervisor, open_window=fake_window, ready_timeout=30)
    try:
        assert shell.run() == 0
    finally:
        app.state.desktop = None
    assert shell.server.url.startswith(f"http://{LOOPBACK}:")
    assert opened and opened[0].startswith(shell.server.url + "/desktop/login?token=")
    assert events == ["window-closed", "worker-stop"], "window first, then the worker, then the server"
    assert not shell.server.running and not supervisor.running
    assert shell.stop() == {"notifications": True, "worker": True, "server": True}, "idempotent"
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{shell.server.url}/health", timeout=1)


def test_desktop_app_does_not_start_the_server_when_not_ready(monkeypatch):
    import app.desktop.launcher as launcher

    monkeypatch.setattr(launcher, "missing_tables", lambda: ["applications"])
    shell = DesktopApp(app, open_window=lambda url: pytest.fail("window must not open"))
    assert shell.run() == 2
    assert shell.readiness is not None and not shell.readiness.ready and not shell.server.running


def test_cli_parser_defaults_are_dry_run_and_window():
    from app.desktop.__main__ import build_parser

    args = build_parser().parse_args([])
    assert args.port is None and not args.worker and not args.headed and not args.no_window
    parsed = build_parser().parse_args(["--worker", "--headed", "--no-window", "--port", "8123"])
    assert parsed.port == 8123 and parsed.worker and parsed.headed and parsed.no_window
