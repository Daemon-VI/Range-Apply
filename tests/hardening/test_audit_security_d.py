"""Audit (security agent D): attack-style checks that the existing suite did not prove.

* The route-guard sweeps in ``test_security.py`` / ``tests/desktop`` iterate
  ``app.routes``; on FastAPI 0.141 that list holds ``_IncludedRouter`` wrappers
  with no ``path``, so those sweeps inspect zero API/dashboard routes. The sweep
  here walks the *effective* routes.
* Cross-site form posts to cookie-authenticated ``/dashboard`` pages (another
  ``127.0.0.1`` port is the same *site*, so ``SameSite=Lax`` does not stop it).
* DNS rebinding against the open read endpoints (Host allowlist, solo mode).
* uvicorn access lines carrying ``?key=`` / ``?token=``.
* Open redirects through ``/desktop/login?next=``.
* Live submission through the API without the typed confirmation.
* Tenant isolation of the desktop pages and the answer bank.
"""

import io
import logging
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from app.config import settings
from app.core.logging import RedactingFilter, configure_logging, redact
from app.core.request_guards import (
    allowed_hosts,
    cross_site_browser_write,
    host_is_allowed,
    host_name,
)
from app.main import app
from app.security import DASHBOARD_COOKIE, DESKTOP_HEADER, DESKTOP_HEADER_VALUE
from tests.conftest import AUTH_HEADERS
from tests.hardening.conftest import as_tenant

# --------------------------------------------------------------- route sweep


def _effective_routes(routes):
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            yield from _effective_routes(route.effective_candidates())
        else:
            yield route


def _guards(route) -> set[str]:
    found: set[str] = set()

    def walk(dependant):
        if dependant is None:
            return
        for dep in dependant.dependencies:
            found.add(getattr(dep.call, "__name__", ""))
            walk(dep)

    walk(getattr(route, "dependant", None))
    return found & {"require_api_key", "require_dashboard_auth"}


def test_route_guard_sweep_walks_nested_routers():
    routes = [(getattr(r, "path", ""), set(getattr(r, "methods", None) or ()), _guards(r)) for r in _effective_routes(app.routes)]
    assert len([p for p, _, _ in routes if p.startswith("/api/")]) > 150, "the sweep must actually see the API routes"
    unguarded_writes = [f"{sorted(m)} {p}" for p, m, g in routes if p.startswith("/api/") and m & {"POST", "PUT", "PATCH", "DELETE"} and "require_api_key" not in g]
    assert unguarded_writes == []
    unguarded_dashboard = [p for p, _, g in routes if p.startswith("/dashboard") and "require_dashboard_auth" not in g]
    assert unguarded_dashboard == []
    desktop_open = {"/desktop/login", "/desktop/open", "/desktop/static"}
    unguarded_desktop = [p for p, _, g in routes if p.startswith("/desktop") and p not in desktop_open and not g]
    assert unguarded_desktop == []


# ------------------------------------------------------ cross-site form posts

DASHBOARD_WRITE = "/dashboard/learning/settings"
FORM = {"min_samples": "5", "prior_strength": "10", "minimum_evidence": "MODERATE"}


def test_cross_site_dashboard_form_post_is_refused_even_with_the_cookie(client):
    client.cookies.set(DASHBOARD_COOKIE, settings.api_key)
    hostile = [
        {"Origin": "https://evil.example"},
        {"Origin": "http://127.0.0.1:5173"},  # another local port: same site, other origin
        {"Origin": "null"},
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "cross-site"},
    ]
    for headers in hostile:
        response = client.post(DASHBOARD_WRITE, data=FORM, headers=headers)
        assert response.status_code == 403, headers
    same_origin = client.post(DASHBOARD_WRITE, data=FORM, headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
    assert same_origin.status_code == 303
    assert client.post(DASHBOARD_WRITE, data=FORM).status_code == 303, "non-browser clients keep working"
    client.cookies.clear()
    assert client.post(DASHBOARD_WRITE, data=FORM, headers={"Origin": "http://testserver"}).status_code == 401


def test_cross_site_guard_only_touches_browser_page_writes():
    evil = {"origin": "https://evil.example", "host": "127.0.0.1:8000"}
    assert cross_site_browser_write("POST", "/dashboard/scheduler/run", "http", evil)
    assert cross_site_browser_write("POST", "/desktop/api/attempts/x/run", "http", evil)
    assert not cross_site_browser_write("GET", "/dashboard/", "http", evil)
    assert not cross_site_browser_write("POST", "/api/v1/execution/claim", "http", {"origin": "chrome-extension://abc", "host": "127.0.0.1:8000"})
    assert not cross_site_browser_write("POST", "/dashboard/x", "http", {"origin": "http://127.0.0.1:8000", "host": "127.0.0.1:8000", "sec-fetch-site": "same-origin"})


# ------------------------------------------------------------ DNS rebinding


def test_rebound_host_names_cannot_read_the_open_endpoints(client, monkeypatch):
    for host in ("evil.example", "evil.example:8000", "127.0.0.1.evil.example", "localhost.evil.example:8000"):
        response = client.get("/api/v1/career/profile", headers={"host": host})
        assert response.status_code == 421, host
        assert "detail" in response.json()
    for host in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000", "LOCALHOST", "testserver"):
        assert client.get("/health", headers={"host": host}).status_code == 200, host
    monkeypatch.setenv("CAREEROS_ALLOWED_HOSTS", "careeros.lan")
    assert client.get("/health", headers={"host": "careeros.lan:8000"}).status_code == 200
    monkeypatch.setattr(settings, "deployment_mode", "hosted")
    assert client.get("/health", headers={"host": "evil.example"}).status_code == 200, "hosted mode names are not known here"


def test_host_allowlist_parsing():
    assert host_name("[::1]:8000") == "::1" and host_name("LocalHost:1") == "localhost" and host_name("::1") == "::1"
    assert allowed_hosts("0.0.0.0") is None and host_is_allowed("anything", None)
    allowed = allowed_hosts("127.0.0.1", extra="")
    assert host_is_allowed("127.0.0.1:9", allowed) and not host_is_allowed("127.0.0.2", allowed) and not host_is_allowed("", allowed)


# ------------------------------------------------------------------ logging


def test_dashboard_key_query_is_redacted():
    line = 'GET /dashboard/?key=s3cr3t-value&x=1 HTTP/1.1'
    assert "s3cr3t-value" not in redact(line) and "x=1" in redact(line)
    assert "tok-value" not in redact("/desktop/login?token=tok-value&next=/desktop/")


def test_uvicorn_access_lines_are_redacted_and_still_format():
    from uvicorn.logging import AccessFormatter

    access = logging.getLogger("uvicorn.access")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    saved = (access.handlers[:], access.filters[:], access.propagate, access.level)
    access.handlers[:] = [handler]
    access.propagate = False
    access.setLevel(logging.INFO)
    try:
        configure_logging("INFO")
        assert any(isinstance(f, RedactingFilter) for f in access.filters)
        access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/dashboard/?key=live-key-123", "1.1", 200)
        access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/desktop/", "1.1", 200)
        out = stream.getvalue()
        assert "live-key-123" not in out and "[REDACTED]" in out
        assert '"GET /desktop/ HTTP/1.1" 200' in out
    finally:
        access.handlers[:], access.filters[:], access.propagate, level = saved
        access.setLevel(level)


# ---------------------------------------------------------- open redirects


def test_desktop_login_next_never_leaves_the_origin(client):
    from app.desktop.launcher import LoginTokens

    previous = getattr(app.state, "desktop", None)
    app.state.desktop = SimpleNamespace(tokens=LoginTokens())
    try:
        for target in ("//evil.example/", "/\\evil.example/", "https://evil.example/", "/\t/evil.example", "http:/evil.example", "/%2F%2Fevil.example", "\\\\evil.example"):
            token = app.state.desktop.tokens.mint()
            response = client.get("/desktop/login", params={"token": token, "next": target})
            assert response.status_code == 303, target
            location = response.headers["location"]
            parsed = urlparse(location)
            assert location.startswith("/") and not location.startswith("//") and "\\" not in location, (target, location)
            assert not parsed.netloc and not parsed.scheme, (target, location)
            assert client.get("/desktop/login", params={"token": token}).status_code == 401, "single use"
    finally:
        app.state.desktop = previous


# ------------------------------------------------------ real-submission API


def test_api_key_callers_cannot_start_a_live_run_without_the_typed_confirmation(client, harness):
    attempt = harness.ready(company="Live Guard Co")
    url = f"/desktop/api/attempts/{attempt.id}/run"
    for body in ({"mode": "live"}, {"mode": "live", "confirm": ""}, {"mode": "live", "confirm": "submit"}, {"mode": "live", "confirm": " SUBMIT"}, {"mode": "live", "confirm": "SUBMIT "}):
        assert client.post(url, json=body, headers=AUTH_HEADERS).status_code == 400, body
    assert client.post(url, json={"mode": "LIVE", "confirm": "SUBMIT"}, headers=AUTH_HEADERS).status_code == 422
    # cookie + desktop header from another origin never reaches the route
    client.cookies.set(DASHBOARD_COOKIE, settings.api_key)
    assert client.post(url, json={"mode": "live", "confirm": "SUBMIT"}, headers={DESKTOP_HEADER: DESKTOP_HEADER_VALUE, "Origin": "http://127.0.0.1:9999"}).status_code == 403
    client.cookies.clear()
    # the legacy v5 route refuses live submission outright
    dry_run_before = settings.playwright_dry_run
    response = client.post(f"/api/v5/applications/{attempt.job_id}/submit?dry_run=false", json={"approved": True}, headers=AUTH_HEADERS)
    assert response.status_code in (404, 409, 422) and response.status_code != 200
    assert settings.playwright_dry_run == dry_run_before
    # the kill switch cannot be flipped anonymously
    assert client.post("/api/v5/applications/killswitch", json={"paused": False}).status_code == 401


def test_no_route_can_turn_off_dry_run_or_change_live_settings():
    offenders = []
    for route in _effective_routes(app.routes):
        endpoint = getattr(route, "endpoint", None)
        code = getattr(endpoint, "__code__", None)
        if code is not None and "playwright_dry_run" in code.co_names and (getattr(route, "methods", None) or set()) & {"POST", "PUT", "PATCH", "DELETE"}:
            offenders.append(getattr(route, "path", ""))
    assert offenders == []


# ----------------------------------------------------------- tenant isolation


@pytest.fixture
def docs_root(tmp_path, monkeypatch):
    root = tmp_path / "documents"
    monkeypatch.setattr(settings, "documents_root", str(root))
    return root


def test_desktop_pages_and_files_never_show_another_tenants_objects(client, harness, other_tenant_id, docs_root):
    attempt = harness.ready(company="Foreign Desk Co")
    harness.execute(attempt)
    artifacts = client.get(f"/api/v1/documents/by-preparation/{attempt.preparation_id}").json()
    assert artifacts
    artifact_id = artifacts[0]["id"]
    client.cookies.set(DASHBOARD_COOKIE, settings.api_key)
    mine = client.get(f"/desktop/documents/{artifact_id}/file")
    assert mine.status_code == 200
    as_tenant(other_tenant_id)
    try:
        assert client.get(f"/desktop/documents/{artifact_id}/file").status_code in (404, 409, 422)
        assert client.get(f"/api/v1/documents/{artifact_id}/file", headers=AUTH_HEADERS).status_code in (404, 409, 422)
        for path in (f"/desktop/applications/{attempt.id}", f"/desktop/applications/{attempt.id}/confirm", f"/desktop/opportunities/{attempt.candidate_opportunity_id}"):
            response = client.get(path)
            assert response.status_code != 200 or "Foreign Desk Co" not in response.text, path
        for path in ("/desktop/applications", "/desktop/opportunities", "/desktop/documents", "/desktop/attention", "/desktop/"):
            response = client.get(path)
            assert response.status_code == 200 and "Foreign Desk Co" not in response.text, path
    finally:
        client.cookies.clear()


def test_answer_bank_entries_are_tenant_scoped(client, tenant_id, other_tenant_id):
    created = client.post("/api/v1/career/answers", json={"category": "work_auth", "question": "Audit D tenant question?", "answer": "Audit D private answer"}, headers=AUTH_HEADERS)
    assert created.status_code == 201, created.text
    entry_id = created.json()["id"]
    as_tenant(other_tenant_id)
    assert client.get(f"/api/v1/career/answers/{entry_id}").status_code == 404
    assert client.post(f"/api/v1/career/answers/{entry_id}/approve", headers=AUTH_HEADERS).status_code == 404
    assert "Audit D private answer" not in client.get("/api/v1/career/answers").text
    as_tenant(tenant_id)
    assert client.get(f"/api/v1/career/answers/{entry_id}").status_code == 200
