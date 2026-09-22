"""Security regression group: tenant isolation / IDOR across every object
family, traversal, credential redaction, CORS, extension message
validation and permissions, the single AI boundary, timing-safe key
comparison, document hash validation, kill switch, submit idempotency."""

import inspect
import re

import pytest

from app.config import PROJECT_ROOT
from app.core.logging import redact
from app.documents.storage import ArtifactStore, StorageError
from tests.conftest import AUTH_HEADERS
from tests.hardening.conftest import as_tenant

EXT = PROJECT_ROOT / "extension"


# --------------------------------------------------------------- IDOR sweep


def test_foreign_ids_never_expose_another_tenants_objects(client, harness, db_session, other_tenant_id, docs_root_for_harness):
    from app.learning.engine import LearningEngine
    from app.signals.models import AttributionHints
    from app.signals.service import SignalInboxService

    attempt = harness.ready(company="Idor Co")
    harness.execute(attempt)
    run_id = attempt.last_execution_id
    item = harness.item(attempt)
    prep_id = attempt.preparation_id
    artifacts = client.get(f"/api/v1/documents/by-preparation/{prep_id}").json()
    assert artifacts, "the executed attempt rendered a document"
    artifact_id = artifacts[0]["id"]
    signal, _ = SignalInboxService(db_session, harness.tenant_id).ingest_email(__import__("tests.signals.conftest", fromlist=["email"]).email("Interview", "We'd like to invite you to an interview.", hints=AttributionHints(application_id=attempt.id)))
    snapshot = LearningEngine(db_session, harness.tenant_id).snapshot(actor="test")
    from app.execution.database.models import FormSnapshotRow

    field_id = db_session.query(FormSnapshotRow).filter_by(application_id=attempt.id).first().fields[0].id
    db_session.commit()
    job_id = attempt.job_id
    co_id = attempt.candidate_opportunity_id

    mine = [
        ("GET", f"/api/v1/execution/attempts/{attempt.id}/runs"), ("GET", f"/api/v1/execution/runs/{run_id}"), ("GET", f"/api/v1/preparations/{prep_id}"),
        ("GET", f"/api/v1/documents/{artifact_id}"), ("GET", f"/api/v1/signals/{signal.id}"), ("GET", f"/api/v1/signals/outcomes/{attempt.id}"),
        ("GET", f"/api/v1/learning/snapshots/{snapshot.id}"), ("GET", f"/api/v1/opportunities/{co_id}"), ("GET", f"/api/v1/queue/{item.id}"), ("GET", f"/api/v5/applications/{job_id}"),
    ]
    for method, url in mine:
        assert client.request(method, url, headers=AUTH_HEADERS).status_code == 200, url

    as_tenant(other_tenant_id)
    foreign = mine + [
        ("GET", f"/api/v1/execution/attempts/{attempt.id}/preview"), ("POST", f"/api/v1/execution/attempts/{attempt.id}/retry"), ("POST", f"/api/v1/execution/attempts/{attempt.id}/cancel"),
        ("POST", f"/api/v1/execution/runs/{run_id}/confirm"), ("POST", f"/api/v1/execution/runs/{run_id}/verify"), ("PUT", f"/api/v1/execution/fields/{field_id}/answer"),
        ("POST", f"/api/v1/execution/items/{item.id}/start"), ("POST", f"/api/v1/preparations/{prep_id}/approve"), ("POST", f"/api/v1/preparations/{prep_id}/invalidate"),
        ("GET", f"/api/v1/documents/{artifact_id}/file"), ("POST", f"/api/v1/documents/{artifact_id}/invalidate"), ("GET", f"/api/v1/signals/{signal.id}/trace"),
        ("POST", f"/api/v1/signals/{signal.id}/ignore"), ("POST", f"/api/v1/learning/snapshots/{snapshot.id}"), ("GET", f"/api/v1/learning/expected/{co_id}"),
        ("POST", f"/api/v1/opportunities/{co_id}/state"), ("POST", f"/api/v1/queue/{item.id}/cancel"),
    ]
    bodies = {"confirm": {"submitted": True}, "answer": {"answer": "x"}, "start": {"worker_id": "w", "executor": "MOCK"}, "state": {"state": "SKIPPED"}, "verify": {}}
    for method, url in foreign:
        body = next((v for k, v in bodies.items() if url.endswith(k)), {})
        response = client.request(method, url, json=body if method != "GET" else None, headers=AUTH_HEADERS)
        assert response.status_code in (404, 405, 409, 422) and response.status_code != 200, f"{method} {url} -> {response.status_code}"
        if response.status_code == 422:
            assert "detail" in response.json() and attempt.id not in response.text
    assert client.get("/api/v1/documents/by-preparation/" + prep_id).json() == []
    assert client.get("/api/v5/applications/").json() == []
    assert client.get("/api/v1/execution/attempts").json()["total"] == 0
    assert client.get("/api/v1/signals").json()["total"] == 0 and client.get("/api/v1/learning/snapshots").json() == []
    as_tenant(harness.tenant_id)
    assert client.get("/api/v5/applications/").json() != []


@pytest.fixture
def docs_root_for_harness(tmp_path, monkeypatch):
    from app.config import settings

    root = tmp_path / "documents"
    monkeypatch.setattr(settings, "documents_root", str(root))
    return root


# -------------------------------------------------------------- traversal


def test_document_paths_refuse_traversal_and_absolute_paths(tmp_path):
    store = ArtifactStore(str(tmp_path))
    for bad in ("../x.pdf", "a/../../x.pdf", "/etc/passwd", "C:/x.pdf", "a\\..\\b.pdf", "", "t/./p/x.pdf"):
        with pytest.raises(StorageError):
            store.absolute(bad)
    with pytest.raises(StorageError):
        store.relative_path("../tenant", "co", "prep", "RESUME", __import__("app.documents.models", fromlist=["DocumentFormat"]).DocumentFormat.PDF, 1)
    good = store.absolute("tenant/co/prep/resume-v1.pdf")
    assert str(good).startswith(str(tmp_path.resolve()))


def test_document_hash_mismatch_is_detected(tmp_path):
    from app.documents.models import DocumentFormat

    store = ArtifactStore(str(tmp_path))
    data = b"%PDF-1.4 hello"
    path = store.write_immutable("t/c/p/resume-v1.pdf", data)
    digest = __import__("hashlib").sha256(data).hexdigest()
    assert store.verify("t/c/p/resume-v1.pdf", digest, len(data), DocumentFormat.PDF) == []
    path.write_bytes(b"%PDF-1.4 tampered")
    problems = store.verify("t/c/p/resume-v1.pdf", digest, len(data), DocumentFormat.PDF)
    assert any("SHA-256" in p for p in problems)
    with pytest.raises(StorageError):
        store.write_immutable("t/c/p/resume-v1.pdf", data)


# ------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    "line",
    [
        "X-API-Key: test-api-key-value",
        "careeros_key=abc123secret; path=/",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.sig_part_here",
        "GEMINI_API_KEY=AIzaSyA1234567890abcdefghijklmnopqrstu",
        "password=hunter2 cookie: session=deadbeef",
    ],
)
def test_secrets_never_survive_the_log_filter(line):
    out = redact(line)
    for secret in ("test-api-key-value", "abc123secret", "eyJhbGciOiJIUzI1NiJ9", "AIzaSyA1234567890", "hunter2", "deadbeef"):
        assert secret not in out


def test_api_key_comparison_is_timing_safe():
    import app.security as sec

    assert "hmac.compare_digest" in inspect.getsource(sec._matches)


# ------------------------------------------------------------------- CORS


def test_cors_grants_only_extension_origins_and_never_wildcard(client):
    for origin in ("https://evil.example", "http://localhost:3000", "null"):
        r = client.options("/api/v1/execution/extension/status", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
        assert "access-control-allow-origin" not in r.headers, origin
    r = client.options("/api/v1/execution/extension/status", headers={"Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop", "Access-Control-Request-Method": "GET"})
    assert r.headers["access-control-allow-origin"] != "*" and r.headers.get("access-control-allow-credentials") is None


# --------------------------------------------------------------- extension


def test_extension_manifest_and_message_guards():
    import json

    manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert set(manifest["permissions"]) <= {"storage", "activeTab", "scripting", "tabs", "alarms"}
    for forbidden in ("cookies", "history", "webRequest", "webRequestBlocking", "webNavigation", "clipboardRead", "nativeMessaging", "debugger"):
        assert forbidden not in manifest["permissions"]
    assert manifest["host_permissions"] == ["http://127.0.0.1/*", "http://localhost/*"]
    assert "externally_connectable" not in manifest and "content_scripts" not in manifest, "the content script is injected on demand, never declared for every page"
    content = (EXT / "src" / "content.js").read_text(encoding="utf-8")
    background = (EXT / "src" / "background.js").read_text(encoding="utf-8")
    assert "sender.id !== chrome.runtime.id" in content and "message.target !== 'content'" in content
    assert "sender.id !== chrome.runtime.id" in background
    for source in (content, background, (EXT / "src" / "fill.js").read_text(encoding="utf-8"), (EXT / "src" / "discover.js").read_text(encoding="utf-8")):
        assert "document.cookie" not in source and "chrome.cookies" not in source and "chrome.history" not in source and "localStorage" not in source
    api = (EXT / "src" / "api.js").read_text(encoding="utf-8")
    assert "X-API-Key" in api and "console.log(" not in api, "the key travels in a header and is never logged"


# --------------------------------------------------------------------- AI


def test_exactly_one_ai_boundary():
    offenders = []
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if rel.startswith("app/ai/"):
            continue
        for needle in ("generativelanguage", "GeminiProvider(", "OllamaProvider(", "build_provider(", "openai", "anthropic", "httpx.post", "requests.post", "api.openai.com"):
            if needle in text:
                offenders.append((rel, needle))
    assert offenders == [], offenders
    gateway_callers = [p.relative_to(PROJECT_ROOT).as_posix() for p in (PROJECT_ROOT / "app").rglob("*.py") if "get_gateway()" in p.read_text(encoding="utf-8") and not p.relative_to(PROJECT_ROOT).as_posix().startswith("app/ai/")]
    assert set(gateway_callers) <= {"app/jobs/extraction/llm_provider.py", "app/preparation/ai.py", "app/signals/ai.py", "app/api/routes/diagnostics.py", "app/pipeline/dashboard/views.py"}, gateway_callers


def test_ai_disabled_regression_makes_zero_provider_calls():
    """The whole default configuration answers DISABLED; the sink counts zero provider calls."""
    from app.ai.gateway import AIGateway, GlobalAIConfig, UsageSink
    from tests.ai.conftest import request

    gateway = AIGateway(config=GlobalAIConfig(enabled=False, persist_usage=False), sink=UsageSink(persist=False))
    for _ in range(50):
        assert gateway.run(request(text="x"), tenant=None).status.value == "DISABLED"
    assert gateway.stats()["provider_calls"] == 0


# ---------------------------------------------------------------- misc


def test_no_raw_sql_string_formatting_in_app():
    pattern = re.compile(r"\btext\(\s*f[\"']|\bexecute\(\s*f[\"']|\.execute\([\"'][^\"']*%s")
    hits = [p.relative_to(PROJECT_ROOT).as_posix() for p in (PROJECT_ROOT / "app").rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))]
    assert hits == [], hits


def _effective_routes(routes):
    """FastAPI stores included routers as wrappers; walk to the routes that actually serve requests (audit 2026-09-14)."""
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            yield from _effective_routes(route.effective_candidates())
        else:
            yield route


def _dependency_names(route) -> str:
    names = []

    def walk(dependant):
        if dependant is None:
            return
        for dep in dependant.dependencies:
            names.append(getattr(dep.call, "__name__", str(dep.call)))
            walk(dep)

    walk(getattr(route, "dependant", None))
    return " ".join(names)


def test_write_endpoints_all_require_the_api_key():
    from app.main import app as fastapi_app

    unprotected, checked = [], 0
    for route in _effective_routes(fastapi_app.routes):
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        if not path.startswith("/api/") or not (methods & {"POST", "PUT", "PATCH", "DELETE"}):
            continue
        checked += 1
        if "require_api_key" not in _dependency_names(route):
            unprotected.append(f"{sorted(methods)} {path}")
    assert checked > 50, "the sweep must see the real routes"
    assert unprotected == [], unprotected


def test_dashboard_routes_all_require_dashboard_auth():
    from app.main import app as fastapi_app

    unprotected, checked = [], 0
    for route in _effective_routes(fastapi_app.routes):
        path = getattr(route, "path", "")
        if not path.startswith("/dashboard") or not hasattr(route, "dependant"):
            continue
        checked += 1
        if "require_dashboard_auth" not in _dependency_names(route):
            unprotected.append(path)
    assert checked > 10, "the sweep must see the real routes"
    assert unprotected == [], unprotected


def test_config_endpoints_expose_no_secrets(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "AIzaSECRETVALUE0000000000000000000000")
    monkeypatch.setattr(settings, "firecrawl_api_key", "fc-SECRETVALUE00000000")
    from app.ai.gateway import reset_gateway

    reset_gateway(None)
    for url in ("/api/v1/ai/config", "/api/v1/ai/status", "/api/v1/ops/diagnostics", "/health", "/api/v1/learning/settings"):
        text = client.get(url).text
        assert "SECRETVALUE" not in text and "test-api-key" not in text, url
    reset_gateway(None)
