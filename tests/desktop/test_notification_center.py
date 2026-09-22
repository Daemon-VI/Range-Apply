"""Increment 5: the in-memory notification center and its in-app presentation.

The center holds nothing but recent :class:`Notification` objects for the life
of the process — no table, no migration, no file — so these tests construct
notifications directly and never go near the derivation (Subagent A) or the
native toast adapter (Subagent B).

Two invariants are checked hard, because they are the ones that would hurt:
a tenant never sees another tenant's notifications, and the single write route
(``POST /desktop/api/notifications/seen``) touches memory only — it can never
start, retry or change an application.
"""

import threading
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.application.database.models import ApplicationRow
from app.config import settings
from app.desktop.notifications import model as nmodel
from app.desktop.notifications.center import NotificationCenter, get_center, reset_center
from app.desktop.notifications.model import Notification
from app.execution.database.models import ExecutionRunRow
from app.main import app
from app.security import DASHBOARD_COOKIE, DESKTOP_HEADER, DESKTOP_HEADER_VALUE
from tests.execution.conftest import harness  # noqa: F401
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
SEEN_URL = "/desktop/api/notifications/seen"

BASE = datetime(2026, 9, 13, 10, 0, 0)


def make(tenant: str, key: str, kind: str = nmodel.APPLICATION_READY, *, body: str = "Your application for Platform Engineer at Desktop Corp is ready for review.", path: str = "/desktop/applications/app-1", at: datetime = BASE, application_id: str = "app-1", reason: str | None = None, importance: str = "normal") -> Notification:
    return Notification(key=key, kind=kind, title=nmodel.TITLES[kind], body=body, path=path, tenant_id=tenant, occurred_at=at, application_id=application_id, reason=reason, importance=importance)


@pytest.fixture(autouse=True)
def fresh_center():
    """Every test starts and ends with an empty process-wide center."""
    reset_center()
    yield
    reset_center()


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


# ------------------------------------------------------------- the center


def test_add_stores_once_and_dedupes_by_key():
    center = NotificationCenter()
    first = make("t1", "attempt:app-1:READY:e1")
    assert center.add(first) is True
    # the same key again changes nothing at all — no re-toast, no re-unseen
    center.mark_seen("t1")
    assert center.add(make("t1", "attempt:app-1:READY:e1", body="different text")) is False
    assert center.recent("t1") == [first]
    assert center.unseen_count("t1") == 0
    # a different key is a different notification
    assert center.add(make("t1", "attempt:app-1:READY:e2")) is True
    assert len(center.recent("t1")) == 2


def test_center_is_bounded_and_drops_the_oldest():
    center = NotificationCenter(maxlen=5)
    for index in range(12):
        assert center.add(make("t1", f"k{index}", at=BASE + timedelta(minutes=index))) is True
    held = center.recent("t1", limit=50)
    assert len(held) == 5
    assert [n.key for n in held] == ["k11", "k10", "k9", "k8", "k7"]
    assert center.count() == 5


def test_recent_is_newest_first_and_respects_the_limit():
    center = NotificationCenter()
    center.add(make("t1", "old", at=BASE))
    center.add(make("t1", "new", at=BASE + timedelta(hours=2)))
    center.add(make("t1", "middle", at=BASE + timedelta(hours=1)))
    assert [n.key for n in center.recent("t1")] == ["new", "middle", "old"]
    assert [n.key for n in center.recent("t1", limit=2)] == ["new", "middle"]
    assert center.recent("t1", limit=0) == []


def test_unseen_count_and_mark_seen_some_then_all():
    center = NotificationCenter()
    for key in ("a", "b", "c"):
        center.add(make("t1", key))
    assert center.unseen_count("t1") == 3
    assert center.mark_seen("t1", ["a", "b"]) == 2
    assert center.unseen_count("t1") == 1
    # idempotent: marking the same keys again changes nothing
    assert center.mark_seen("t1", ["a", "b"]) == 0
    # unknown keys are ignored rather than an error
    assert center.mark_seen("t1", ["nope"]) == 0
    assert center.mark_seen("t1") == 1
    assert center.unseen_count("t1") == 0
    center.clear()
    assert center.recent("t1") == [] and center.unseen_count("t1") == 0


def test_a_tenant_never_sees_or_marks_another_tenants_notifications():
    center = NotificationCenter()
    center.add(make("t1", "mine"))
    center.add(make("t2", "theirs", body="Your application for Secret Role at Other Tenant Inc is ready for review."))
    assert [n.key for n in center.recent("t1")] == ["mine"]
    assert [n.key for n in center.recent("t2")] == ["theirs"]
    assert center.unseen_count("t1") == 1 and center.unseen_count("t2") == 1
    # marking everything for t1 leaves t2 untouched, by key as well as by "all"
    assert center.mark_seen("t1", ["theirs"]) == 0
    assert center.mark_seen("t1") == 1
    assert center.unseen_count("t2") == 1
    assert center.count("t1") == 1 and center.count("t2") == 1
    assert center.recent("t3") == [] and center.unseen_count("t3") == 0


def test_concurrent_adds_and_marks_stay_consistent():
    center = NotificationCenter(maxlen=500)
    added: list[bool] = []
    lock = threading.Lock()

    def worker(worker_index: int):
        results = []
        for index in range(40):
            # every thread tries the SAME 40 keys: exactly 40 adds may win
            results.append(center.add(make("t1", f"k{index}", at=BASE + timedelta(seconds=index))))
            center.unseen_count("t1")
            center.recent("t1", limit=5)
        with lock:
            added.extend(results)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(1 for ok in added if ok) == 40
    assert center.count("t1") == 40
    assert center.mark_seen("t1") == 40
    assert center.unseen_count("t1") == 0


def test_get_center_is_a_singleton_that_reset_center_forgets():
    first = get_center()
    assert first is get_center()
    first.add(make("t1", "k"))
    reset_center()
    assert get_center() is not first
    assert get_center().recent("t1") == []


def test_a_notification_with_a_non_local_path_cannot_be_constructed():
    for bad in ("https://boards.greenhouse.io/acme/jobs/1", "//evil.test/x", "/desktop/applications/a?key=secret", "/desktop/applications/a#x", "desktop/applications/a"):
        with pytest.raises(ValueError):
            make("t1", "k", path=bad)
    with pytest.raises(ValueError):
        Notification(key="k", kind="NOT_A_KIND", title="x", body="y", path="/desktop/", tenant_id="t1", occurred_at=BASE)
    with pytest.raises(ValueError):
        make("t1", "k", body="x" * (nmodel.MAX_BODY + 1))


# ---------------------------------------------------------------- the page


def test_notifications_page_and_partial_require_dashboard_auth():
    anonymous = TestClient(app, follow_redirects=False)
    assert anonymous.get("/desktop/notifications").status_code == 401
    assert anonymous.get("/desktop/partials/notifications").status_code == 401


def test_page_lists_this_tenants_notifications_only_with_an_open_link(client, tenant_id, other_tenant_id, ready):  # noqa: F811
    center = get_center()
    center.add(make(tenant_id, "attempt:ready:1", path=f"/desktop/applications/{ready.id}", application_id=ready.id))
    center.add(make(other_tenant_id, "attempt:foreign:1", body="Your application for Secret Role at Other Tenant Inc is ready for review.", path="/desktop/applications/foreign-1", application_id="foreign-1"))
    page = client.get("/desktop/notifications")
    assert page.status_code == 200
    html = page.text
    assert "Application Ready" in html and "Desktop Corp" in html
    assert "Other Tenant Inc" not in html and "foreign-1" not in html
    assert f'href="/desktop/applications/{ready.id}">Open<' in html
    assert f'data-url="{SEEN_URL}"' in html and "Mark all as seen" in html
    partial = client.get("/desktop/partials/notifications")
    assert partial.status_code == 200
    assert "Desktop Corp" in partial.text and "Other Tenant Inc" not in partial.text
    # the page polls the partial rather than reloading itself
    assert 'hx-get="/desktop/partials/notifications"' in html and 'hx-trigger="every 30s' in html
    # no credential is ever rendered
    for body in (html, partial.text):
        assert settings.api_key not in body and "X-API-Key" not in body


def test_empty_state_explains_the_native_and_in_app_channels(client):
    html = client.get("/desktop/notifications").text
    assert "Nothing has been announced yet." in html
    assert "winotify" in html and "toast" in html
    assert "listed here" in html


def test_sidebar_badge_counts_unseen_and_drops_after_marking_seen(client, tenant_id):  # noqa: F811
    center = get_center()
    for key in ("a", "b"):
        center.add(make(tenant_id, key, at=BASE))
    home = client.get("/desktop/")
    assert home.status_code == 200
    assert 'href="/desktop/notifications"' in home.text
    assert 'Notifications <span class="badge badge-blue">2</span>' in home.text
    response = client.post(SEEN_URL, json={}, headers=DESKTOP)
    assert response.status_code == 200 and response.json() == {"marked": 2, "unseen": 0}
    assert 'class="badge badge-blue">2</span>' not in client.get("/desktop/").text
    assert center.unseen_count(tenant_id) == 0


def test_seen_route_marks_only_the_named_keys_and_only_this_tenant(client, tenant_id, other_tenant_id):  # noqa: F811
    center = get_center()
    for key in ("a", "b", "c"):
        center.add(make(tenant_id, key))
    center.add(make(other_tenant_id, "a"))
    response = client.post(SEEN_URL, json={"keys": ["a"]}, headers=DESKTOP)
    assert response.status_code == 200 and response.json() == {"marked": 1, "unseen": 2}
    assert center.unseen_count(other_tenant_id) == 1, "another tenant's identically keyed entry is untouched"
    assert client.post(SEEN_URL, json={"keys": []}, headers=DESKTOP).json() == {"marked": 0, "unseen": 2}
    assert client.post(SEEN_URL, json={}, headers=DESKTOP).json() == {"marked": 2, "unseen": 0}


# ----------------------------------------------------------------- the auth


def test_seen_route_needs_the_cookie_and_the_desktop_header_together(client, tenant_id):  # noqa: F811
    get_center().add(make(tenant_id, "a"))
    # cookie only (a plain browser tab, no script): refused
    assert client.post(SEEN_URL, json={}).status_code == 401
    # header only (no cookie): refused
    cookieless = TestClient(app, follow_redirects=False)
    assert cookieless.post(SEEN_URL, json={}, headers=DESKTOP).status_code == 401
    assert get_center().unseen_count(tenant_id) == 1, "nothing was marked by the refused calls"
    # cookie + header (what desktop.js sends): accepted
    accepted = client.post(SEEN_URL, json={}, headers=DESKTOP)
    assert accepted.status_code == 200 and accepted.json() == {"marked": 1, "unseen": 0}


def test_seen_route_still_accepts_the_plain_api_key_path(client, tenant_id):  # noqa: F811
    get_center().add(make(tenant_id, "a"))
    keyed = TestClient(app, follow_redirects=False).post(SEEN_URL, json={}, headers={"X-API-Key": settings.api_key})
    assert keyed.status_code == 200 and keyed.json() == {"marked": 1, "unseen": 0}


# ------------------------------------------------------- nothing is started


def test_seen_route_touches_no_database_row_and_starts_nothing(client, db_session, tenant_id, ready):  # noqa: F811
    center = get_center()
    center.add(make(tenant_id, "attempt:ready:1", path=f"/desktop/applications/{ready.id}", application_id=ready.id))
    before_status = ready.status
    before_updated = ready.updated_at
    runs_before = db_session.query(ExecutionRunRow).filter(ExecutionRunRow.tenant_id == tenant_id).count()
    attempts_before = db_session.query(ApplicationRow).filter(ApplicationRow.tenant_id == tenant_id).count()

    assert client.post(SEEN_URL, json={}, headers=DESKTOP).status_code == 200

    db_session.expire_all()
    refreshed = db_session.get(ApplicationRow, ready.id)
    assert refreshed.status == before_status and refreshed.updated_at == before_updated
    assert db_session.query(ExecutionRunRow).filter(ExecutionRunRow.tenant_id == tenant_id).count() == runs_before
    assert db_session.query(ApplicationRow).filter(ApplicationRow.tenant_id == tenant_id).count() == attempts_before


def _all_routes(container=None):
    """Every APIRoute, flattened: this FastAPI wraps included routers."""
    out = []
    for route in getattr(container if container is not None else app, "routes", []):
        if hasattr(route, "dependant"):
            out.append(route)
        nested = getattr(route, "original_router", None) or (route if getattr(route, "routes", None) and route is not container else None)
        if nested is not None:
            out.extend(_all_routes(nested))
    return out


def test_the_only_notification_route_is_the_in_memory_seen_write():
    routes = {}
    for route in _all_routes():
        path = getattr(route, "path", "")
        if path.startswith("/desktop/api/notifications"):
            routes[path] = set(getattr(route, "methods", set()) or set()) - {"HEAD", "OPTIONS"}
    assert routes == {SEEN_URL: {"POST"}}, routes
    # and it declares no database session, so it cannot reach the application
    seen = next(r for r in _all_routes() if getattr(r, "path", "") == SEEN_URL)
    guards = " ".join(str(d.call) for d in seen.dependant.dependencies)
    flat = guards + " " + " ".join(str(d.call) for d in seen.dependant.dependencies + [seen.dependant])
    assert "require_api_key" in guards
    assert "get_db" not in flat and "ExecutionService" not in flat
