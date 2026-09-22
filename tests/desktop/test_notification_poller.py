"""Increment 5: the poller loop, its supervisor wiring and the click route.

Derivation is faked here (it has its own tests): these tests prove the loop
dedupes across polls and restarts, survives a bad poll, hands a native toast
a local click URL without any credential, keeps running as a supervised
thread inside ``DesktopApp`` and stops before the server does, and that
``GET /desktop/open`` only ever navigates the window to a local page.
"""

import logging
import threading
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.desktop.launcher import DesktopApp, LoginTokens
from app.desktop.notifications import model
from app.desktop.notifications.center import NotificationCenter, get_center, reset_center
from app.desktop.notifications.cursor import Cursor, CursorStore
from app.desktop.notifications.poller import NotificationPoller
from app.desktop.services import WorkerSupervisor, default_poller_factory
from app.main import app

TENANT = "poller-tenant"


def _notification(key: str, kind: str = model.APPLICATION_READY, application_id: str = "app-1") -> model.Notification:
    return model.Notification(key=key, kind=kind, title=model.TITLES[kind], body="Your application for Engineer at Acme is ready for review.", path=f"/desktop/applications/{application_id}", tenant_id=TENANT, occurred_at=datetime.now(timezone.utc), application_id=application_id)


class FakeNotifier:
    name = "fake"
    available = True

    def __init__(self, fail: bool = False):
        self.sent: list[tuple[model.Notification, str | None]] = []
        self.fail = fail

    def send(self, notification, launch_url=None) -> bool:
        self.sent.append((notification, launch_url))
        return not self.fail


class ScriptedDerive:
    """Returns the queued batches one poll at a time, then nothing."""

    def __init__(self, *batches):
        self.batches = list(batches)
        self.calls = 0

    def __call__(self, db, tenant_id, cursor, now=None):
        self.calls += 1
        batch = self.batches.pop(0) if self.batches else []
        if isinstance(batch, Exception):
            raise batch
        return list(batch), cursor


class FakeSession:
    closed = 0

    def close(self):
        FakeSession.closed += 1


@pytest.fixture
def center():
    reset_center()
    yield get_center()
    reset_center()


@pytest.fixture
def store(tmp_path):
    return CursorStore(tmp_path / "notifications.json")


def _poller(store, center, derive, notifier=None, launch=None, interval=1.0):
    return NotificationPoller(TENANT, store, center=center, notifier=notifier or FakeNotifier(), interval=interval, launch_url_for=launch, session_factory=FakeSession, derive=derive)


def test_poll_once_notifies_new_items_once_and_advances_the_cursor(store, center):
    n1, n2 = _notification("k1"), _notification("k2", model.ATTENTION_REQUIRED)
    notifier = FakeNotifier()
    poller = _poller(store, center, ScriptedDerive([n1, n2], [n1, n2]), notifier)
    assert poller.poll_once() == [n1, n2]
    assert poller.poll_once() == [], "the same keys never notify twice"
    assert [n.key for n, _ in notifier.sent] == ["k1", "k2"]
    assert poller.totals["polls"] == 2 and poller.totals["notified"] == 2 and poller.totals["native"] == 2
    assert store.path.exists(), "the cursor is saved after every poll"
    assert isinstance(store.load(TENANT), Cursor)
    assert center.unseen_count(TENANT) == 2
    assert FakeSession.closed >= 2, "every poll closes its session"


def test_native_failure_still_leaves_the_notification_in_the_app(store, center):
    poller = _poller(store, center, ScriptedDerive([_notification("k1")]), FakeNotifier(fail=True))
    assert len(poller.poll_once()) == 1
    assert poller.totals["native"] == 0 and center.unseen_count(TENANT) == 1


def test_click_url_is_local_carries_a_single_use_nonce_and_no_secret(store, center):
    nonces = LoginTokens()
    poller = _poller(store, center, ScriptedDerive([_notification("k1")]), launch=lambda n: f"http://127.0.0.1:1{'/desktop/open'}?to={n.path}&n={nonces.mint()}")
    poller.poll_once()
    url = poller.notifier.sent[0][1]
    assert url.startswith("http://127.0.0.1:1/desktop/open?to=/desktop/applications/app-1&n=")
    assert settings.api_key not in url and "token=" not in url and "key=" not in url
    assert nonces.consume(url.rsplit("n=", 1)[1]) is True and nonces.consume(url.rsplit("n=", 1)[1]) is False


def test_a_failing_launch_url_builder_never_loses_the_toast(store, center):
    def broken(_):
        raise RuntimeError("no server yet")

    poller = _poller(store, center, ScriptedDerive([_notification("k1")]), launch=broken)
    poller.poll_once()
    assert poller.notifier.sent[0][1] is None and center.unseen_count(TENANT) == 1


def test_run_loop_survives_a_bad_poll_and_stops_promptly(store, center, caplog):
    derive = ScriptedDerive(RuntimeError("database locked"), [_notification("k1")], [])
    poller = _poller(store, center, derive, interval=1.0)
    supervisor = WorkerSupervisor(lambda: poller, name="test-notifier")
    with caplog.at_level(logging.WARNING):
        assert supervisor.start() is True
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and poller.totals["notified"] < 1:
            time.sleep(0.05)
        started = time.monotonic()
        assert supervisor.stop(timeout=5) is True
    assert time.monotonic() - started < 3, "stop does not wait for the whole interval more than once"
    assert poller.totals["errors"] == 1 and poller.totals["last_error"] == "RuntimeError"
    assert poller.totals["notified"] == 1
    assert "database locked" not in caplog.text and "RuntimeError" in caplog.text, "only the exception type is logged"
    assert supervisor.status()["state"] == "stopped" and supervisor.status()["totals"]["polls"] >= 1


def test_restart_with_the_same_cursor_file_does_not_reannounce(store, tmp_path):
    reset_center()
    n1 = _notification("k1")
    first = _poller(store, get_center(), ScriptedDerive([n1]))
    first.poll_once()
    saved = store.load(TENANT)
    reset_center()  # a restart loses the in-memory center...
    seen_cursor = []

    def derive(db, tenant_id, cursor, now=None):
        seen_cursor.append(cursor)
        return [], cursor

    second = _poller(store, get_center(), derive)
    assert second.poll_once() == []
    assert seen_cursor == [saved], "...but the next poller starts from the saved cursor, so derivation never re-reads announced rows"
    assert store.load(TENANT) == saved


def test_desktop_app_supervises_the_poller_and_stops_it_before_the_server(monkeypatch, tmp_path):
    events = []

    class Recording(WorkerSupervisor):
        def stop(self, timeout=30.0):
            events.append(self._name)
            return super().stop(timeout)

    reset_center()
    store = CursorStore(tmp_path / "n.json")
    poller = _poller(store, get_center(), ScriptedDerive([]), interval=1.0)
    notifier_supervisor = Recording(lambda: poller, name="careeros-notifier")
    worker = Recording(lambda: type("W", (), {"run": lambda self: None, "stop": lambda self: None})(), name="careeros-worker")
    shell = DesktopApp(app, worker_supervisor=worker, notification_supervisor=notifier_supervisor, ready_timeout=30)
    try:
        assert shell.start() is True
        assert shell.notifications.running
        status = shell.status()
        assert status["notifications"]["state"] == "running" and status["window"] is False
        assert settings.api_key not in str(status)
    finally:
        outcome = shell.stop()
        app.state.desktop = None
    assert outcome == {"notifications": True, "worker": True, "server": True}
    assert events[:2] == ["careeros-notifier", "careeros-worker"], "notifications stop first, then the worker, then the server"
    reset_center()


def test_default_poller_factory_builds_a_real_poller_with_local_click_urls(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "desktop_state_dir", str(tmp_path))
    reset_center()

    class Shell:
        server = type("S", (), {"url": "http://127.0.0.1:4321"})()
        open_nonces = LoginTokens()

    poller = default_poller_factory(Shell(), interval=3, prefer_native=False)()
    assert isinstance(poller, NotificationPoller) and poller.interval == 3 and poller.notifier.available is False
    url = poller._launch_url_for(_notification("k1"))
    assert url.startswith("http://127.0.0.1:4321/desktop/open?to=/desktop/applications/app-1&n=") and len(Shell.open_nonces) == 1
    assert str(poller.store.path).startswith(str(tmp_path))
    reset_center()


# ------------------------------------------------------------ click route


@pytest.fixture
def shell_client():
    shell = DesktopApp(app)
    app.state.desktop = shell
    try:
        yield shell, TestClient(app, follow_redirects=False)
    finally:
        app.state.desktop = None


def test_open_route_navigates_the_window_to_a_local_path_only(shell_client, monkeypatch):
    shell, client = shell_client
    navigated = []
    monkeypatch.setattr("app.desktop.window.navigate", lambda url: navigated.append(url) or True)
    monkeypatch.setattr(shell, "_open_window", object())
    good = client.get(f"/desktop/open?to=/desktop/applications/app-1&n={shell.open_nonces.mint()}")
    assert good.status_code == 200 and "CareerOS" in good.text
    assert navigated == [f"{shell.server.url}/desktop/applications/app-1"]
    assert client.get("/desktop/open?to=/desktop/applications/app-1&n=bogus").status_code == 403, "unknown nonce"
    for evil in ("//evil.example/", "https://evil.example/", "/api/v1/attempts/x/run", "/desktop/../x", "/desktop/applications/a?key=b"):
        response = client.get(f"/desktop/open?to={evil}&n={shell.open_nonces.mint()}")
        assert response.status_code == 400, evil
    assert len(navigated) == 1, "nothing else navigated"
    assert settings.api_key not in good.text


def test_open_route_redirects_when_there_is_no_window_and_never_sets_a_cookie(shell_client):
    shell, client = shell_client
    response = client.get(f"/desktop/open?to=/desktop/attention&n={shell.open_nonces.mint()}")
    assert response.status_code == 303 and response.headers["location"] == "/desktop/attention"
    assert "set-cookie" not in response.headers, "a click never signs anyone in"
    assert client.get("/desktop/open?to=/desktop/attention").status_code == 403, "no nonce, no navigation"


def test_open_route_is_a_read_only_get(shell_client):
    shell, client = shell_client
    assert client.post(f"/desktop/open?to=/desktop/attention&n={shell.open_nonces.mint()}").status_code == 405


def test_nonces_are_bounded_and_single_use():
    tokens = LoginTokens(max_size=3)
    minted = [tokens.mint() for _ in range(5)]
    assert len(tokens) == 3
    assert tokens.consume(minted[0]) is False and tokens.consume(minted[4]) is True and tokens.consume(minted[4]) is False


def test_center_is_the_dedupe_boundary_between_threads(store):
    reset_center()
    center: NotificationCenter = get_center()
    n = _notification("shared")
    results = []
    threads = [threading.Thread(target=lambda: results.append(center.add(n))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1
    reset_center()
