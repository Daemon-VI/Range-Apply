"""Security tests: API-key protection, dashboard auth, and error hygiene.

Single-user product (see ``app/security.py``): one shared ``X-API-Key`` guards
every write endpoint and the whole HTML dashboard. Everything read-only stays
open, since there is nothing to protect by hiding GETs on a free-tier,
single-user deployment.
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.intelligence.database.models  # noqa: F401 - registers Phase 3 tables
from app.config import Settings, settings
from app.database import get_db
from app.jobs.database.models import Base
from app.main import app
from app.security import DASHBOARD_COOKIE
from tests.conftest import AUTH_HEADERS, TEST_API_KEY

_DB_PATH = "./test_api_security_runner.db"
_engine = create_engine(f"sqlite:///{_DB_PATH}", connect_args={"check_same_thread": False})
_SessionLocal = sessionmaker(bind=_engine)


def _override_get_db():
    db = _SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="module", autouse=True)
def _security_test_db():
    """A private, migrated-shaped database for this module, cleanly scoped.

    ``app.dependency_overrides`` is process-global, so the previous value (if
    any, from another test module) is restored on teardown rather than just
    popped, to avoid leaking state into whichever module runs next.
    """
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override_get_db
    yield
    if previous is not None:
        app.dependency_overrides[get_db] = previous
    else:
        app.dependency_overrides.pop(get_db, None)
    Base.metadata.drop_all(bind=_engine)
    _engine.dispose()
    if os.path.exists(_DB_PATH):
        try:
            os.remove(_DB_PATH)
        except OSError:
            pass


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


async def _noop_background(*args, **kwargs) -> None:
    """Stand-in for the real background job, so the 202-path test does not
    reach out to a live source adapter or scoring engine at all."""
    return None


# --- write-endpoint protection --------------------------------------------


def test_discovery_run_requires_api_key(client, monkeypatch):
    monkeypatch.setattr("app.jobs.api.routes._execute_discovery", _noop_background)
    payload = {"source": "GREENHOUSE", "identifier": "sec-test-co"}

    resp = client.post("/api/v2/discovery/run", json=payload)
    assert resp.status_code == 401

    resp = client.post(
        "/api/v2/discovery/run", json=payload, headers={"X-API-Key": "definitely-wrong"}
    )
    assert resp.status_code == 401

    resp = client.post("/api/v2/discovery/run", json=payload, headers=AUTH_HEADERS)
    assert resp.status_code == 202


def test_matches_recalculate_requires_api_key(client, monkeypatch):
    monkeypatch.setattr("app.intelligence.api.routes._background_match", _noop_background)

    resp = client.post("/api/v3/matches/recalculate", json={})
    assert resp.status_code == 401

    resp = client.post(
        "/api/v3/matches/recalculate", json={}, headers={"X-API-Key": "definitely-wrong"}
    )
    assert resp.status_code == 401

    resp = client.post("/api/v3/matches/recalculate", json={}, headers=AUTH_HEADERS)
    assert resp.status_code == 202


# --- read endpoints stay open ----------------------------------------------


def test_read_endpoints_stay_open(client):
    resp = client.get("/api/v2/jobs")
    assert resp.status_code == 200

    resp = client.get("/health")
    assert resp.status_code == 200


# --- dashboard auth ---------------------------------------------------------


def test_dashboard_requires_auth_then_accepts_key_and_cookie(client):
    resp = client.get("/dashboard/")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("text/html")
    assert "sign in" in resp.text.lower()
    assert DASHBOARD_COOKIE not in resp.cookies

    resp = client.get("/dashboard/", params={"key": TEST_API_KEY})
    assert resp.status_code == 200
    assert DASHBOARD_COOKIE in resp.cookies

    # The client's cookie jar now carries careeros_key; a follow-up request
    # with no ?key at all must still be authorized by the cookie alone.
    resp = client.get("/dashboard/")
    assert resp.status_code == 200


def test_dashboard_rejects_wrong_key(client):
    resp = client.get("/dashboard/", params={"key": "definitely-wrong"})
    assert resp.status_code == 401


# --- cross-cutting response hygiene ----------------------------------------


def test_every_response_carries_a_request_id(client):
    for resp in (client.get("/health"), client.get("/api/v2/jobs"), client.get("/dashboard/")):
        assert resp.headers.get("X-Request-ID"), f"missing X-Request-ID on {resp.request.url}"


def test_unhandled_error_returns_json_with_request_id_and_no_traceback():
    """An unhandled exception must never leak internals to the client."""

    @app.get("/__test_only_boom__")
    def _boom():
        raise RuntimeError("deliberate failure for test_api_security")

    try:
        with TestClient(app, raise_server_exceptions=False) as raising_client:
            resp = raising_client.get("/__test_only_boom__")
    finally:
        # Clean up: drop the temporary route so it does not leak into other tests.
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", None) != "/__test_only_boom__"
        ]

    assert resp.status_code == 500
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["detail"] == "Internal server error"
    assert body.get("request_id")

    raw = resp.text
    assert "Traceback" not in raw
    assert "RuntimeError" not in raw
    assert "deliberate failure" not in raw


def test_debug_defaults_to_false():
    # The already-configured singleton (no DEBUG env var is set anywhere in
    # the test environment)...
    assert settings.debug is False
    # ...and the class default itself, independent of what happens to be in
    # the environment right now.
    assert Settings.model_fields["debug"].default is False
