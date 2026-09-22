"""Increment 2: cookie-authenticated desktop writes.

A write is authorized by ``X-API-Key`` (unchanged) OR by the dashboard cookie
together with the exact ``X-Requested-With: careeros-desktop`` header. The
header is request-context protection, never a credential; the cookie alone
never writes; cross-site requests are refused; tenant resolution and
endpoint authorization are untouched.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_session_factory
from app.main import app
from app.security import DASHBOARD_COOKIE, DESKTOP_HEADER, DESKTOP_HEADER_VALUE

WRITE = "/api/v1/ai/cache/clear"  # harmless, key-gated write
DESKTOP = {DESKTOP_HEADER: DESKTOP_HEADER_VALUE}


@pytest.fixture
def client():
    return TestClient(app, follow_redirects=False)


def _cookie(value):
    return {DASHBOARD_COOKIE: value}


# ----------------------------------------------------------- the matrix


def test_valid_cookie_with_the_desktop_header_is_allowed(client):
    response = client.post(WRITE, cookies=_cookie(settings.api_key), headers=DESKTOP)
    assert response.status_code == 200 and "removed" in response.json()


def test_valid_cookie_without_the_header_is_rejected(client):
    assert client.post(WRITE, cookies=_cookie(settings.api_key)).status_code == 401


def test_valid_cookie_with_a_wrong_header_is_rejected(client):
    for wrong in ("XMLHttpRequest", "careeros-desktop ", "CAREEROS-DESKTOP", "", "careeros"):
        response = client.post(WRITE, cookies=_cookie(settings.api_key), headers={DESKTOP_HEADER: wrong})
        assert response.status_code == 401, wrong


def test_header_without_a_cookie_is_rejected(client):
    assert client.post(WRITE, headers=DESKTOP).status_code == 401


def test_header_with_an_invalid_cookie_is_rejected(client):
    for bad in ("wrong-key", "", settings.api_key + "x", settings.api_key[:-1]):
        assert client.post(WRITE, cookies=_cookie(bad), headers=DESKTOP).status_code == 401, bad


def test_api_key_header_still_works_with_or_without_the_desktop_header(client):
    assert client.post(WRITE, headers={"X-API-Key": settings.api_key}).status_code == 200
    assert client.post(WRITE, headers={"X-API-Key": settings.api_key, DESKTOP_HEADER: "anything"}).status_code == 200
    assert client.post(WRITE, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post(WRITE).status_code == 401
    # a wrong key header does not fall back to a bare cookie
    assert client.post(WRITE, headers={"X-API-Key": "wrong"}, cookies=_cookie(settings.api_key)).status_code == 401


def test_extension_path_is_unchanged(client):
    assert client.get("/api/v1/execution/extension/status", headers={"X-API-Key": settings.api_key}).status_code == 200
    assert client.get("/api/v1/execution/extension/status").status_code == 401
    # the extension never sends the desktop header; a cookie + header would be refused across origins anyway
    preflight = client.options(
        "/api/v1/execution/extension/status",
        headers={"Origin": "chrome-extension://abcdefghijklmnop", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "x-api-key"},
    )
    assert preflight.status_code == 200 and preflight.headers.get("access-control-allow-origin") == "chrome-extension://abcdefghijklmnop"


# -------------------------------------------------------- csrf / origin


def test_cross_site_requests_are_rejected_even_with_cookie_and_header(client):
    headers = {**DESKTOP, "Origin": "https://evil.example"}
    assert client.post(WRITE, cookies=_cookie(settings.api_key), headers=headers).status_code == 401
    headers = {**DESKTOP, "Sec-Fetch-Site": "cross-site"}
    assert client.post(WRITE, cookies=_cookie(settings.api_key), headers=headers).status_code == 401
    headers = {**DESKTOP, "Sec-Fetch-Site": "same-site"}
    assert client.post(WRITE, cookies=_cookie(settings.api_key), headers=headers).status_code == 401
    # what the desktop page itself sends
    headers = {**DESKTOP, "Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
    assert client.post(WRITE, cookies=_cookie(settings.api_key), headers=headers).status_code == 200


def test_cors_does_not_open_the_desktop_header_to_any_origin(client):
    for origin in ("https://evil.example", "chrome-extension://abcdefghijklmnop"):
        preflight = client.options(
            WRITE,
            headers={"Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-requested-with"},
        )
        assert preflight.status_code != 200 or "x-requested-with" not in (preflight.headers.get("access-control-allow-headers") or "").lower(), origin
        assert preflight.headers.get("access-control-allow-credentials") is None


def test_dashboard_cookie_keeps_samesite_lax_and_httponly(client):
    from app.desktop.launcher import DesktopApp

    shell = DesktopApp(app, open_window=lambda url: None)
    app.state.desktop = shell
    try:
        path = shell.login_url().split(shell.server.url, 1)[1]
        set_cookie = client.get(path).headers["set-cookie"].lower()
    finally:
        app.state.desktop = None
    assert "samesite=lax" in set_cookie and "httponly" in set_cookie and f"{DASHBOARD_COOKIE}=" in set_cookie


# ------------------------------------------------- semantics untouched


def test_desktop_auth_cannot_bypass_endpoint_authorization(client):
    missing = uuid.uuid4().hex
    via_cookie = client.post(f"/api/v1/execution/attempts/{missing}/retry", cookies=_cookie(settings.api_key), headers=DESKTOP)
    via_key = client.post(f"/api/v1/execution/attempts/{missing}/retry", headers={"X-API-Key": settings.api_key})
    assert via_cookie.status_code == via_key.status_code == 404
    # a validation failure is still a validation failure
    bad = client.post("/api/v1/career/answers", json={"category": ""}, cookies=_cookie(settings.api_key), headers=DESKTOP)
    assert bad.status_code == 422


def test_desktop_auth_cannot_cross_tenants(client):
    from app.career.database.models import AnswerBankEntryRow

    question = f"Desktop tenant probe {uuid.uuid4().hex[:8]}?"
    body = {"category": "other", "question": question, "answer": "same tenant as always"}
    response = client.post(
        "/api/v1/career/answers?tenant_id=someone-else",
        json=body,
        cookies=_cookie(settings.api_key),
        headers={**DESKTOP, "X-Tenant-Id": "someone-else"},
    )
    assert response.status_code in (200, 201), response.text
    session = get_session_factory()()
    try:
        row = session.query(AnswerBankEntryRow).filter(AnswerBankEntryRow.question == question).one()
        assert row.tenant_id == settings.default_tenant_id
    finally:
        session.close()


def test_reads_are_unchanged(client):
    assert client.get("/api/v1/opportunities/summary").status_code == 200
    assert client.get("/api/v1/opportunities/summary", cookies=_cookie("nonsense"), headers=DESKTOP).status_code == 200


def test_development_without_a_key_keeps_the_existing_open_behaviour(client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", None)
    monkeypatch.setattr(settings, "app_env", "development")
    assert client.post(WRITE).status_code == 200
    monkeypatch.setattr(settings, "app_env", "production")
    assert client.post(WRITE, cookies=_cookie("whatever"), headers=DESKTOP).status_code == 503
