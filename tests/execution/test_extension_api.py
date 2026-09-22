"""The extension's HTTP surface, driven exactly as background.js drives it."""

import hashlib

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.application.models import ApplicationStatus
from app.execution.executors.extension import BrowserExtensionExecutor
from app.execution.models import (
    ApplicationMethod,
    ATSFamily,
    ExecutionOutcome,
    ExecutionPackage,
    ExecutionResult,
    ExecutionTarget,
    ExecutorKind,
    VerificationMethod,
    VerificationStatus,
)
from app.execution.service import _normalize_url
from app.main import app
from tests.conftest import AUTH_HEADERS

EXT_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


@pytest.fixture
def client(harness):
    previous = app.dependency_overrides.get(get_tenant_id)
    app.dependency_overrides[get_tenant_id] = lambda: harness.tenant_id
    harness.session.commit()
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        if previous is not None:
            app.dependency_overrides[get_tenant_id] = previous
        else:
            app.dependency_overrides.pop(get_tenant_id, None)


def _headers(worker="ext-a"):
    return {**AUTH_HEADERS, "Origin": EXT_ORIGIN}


# ------------------------------------------------------------ transport


def test_cors_allows_extension_origins_only(client):
    ok = client.options("/api/v1/execution/extension/status", headers={"Origin": EXT_ORIGIN, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "x-api-key"})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == EXT_ORIGIN
    assert "x-api-key" in ok.headers["access-control-allow-headers"].lower()
    moz = client.options("/api/v1/execution/extension/status", headers={"Origin": "moz-extension://0f4b2a5e-1234-4c8f-9d1e-abcdefabcdef", "Access-Control-Request-Method": "GET"})
    assert moz.status_code == 200 and moz.headers["access-control-allow-origin"].startswith("moz-extension://")
    web = client.options("/api/v1/execution/extension/status", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in web.headers
    got = client.get("/api/v1/execution/extension/status", headers={"Origin": "https://evil.example", **AUTH_HEADERS})
    assert "access-control-allow-origin" not in got.headers, "a web page never gets a CORS grant"


def test_status_requires_the_api_key_and_reports_the_queue(client, harness):
    assert client.get("/api/v1/execution/extension/status").status_code == 401
    harness.ready(company="Ext Status", application_url="https://jobs.example.com/status/apply")
    harness.session.commit()
    status = client.get("/api/v1/execution/extension/status", params={"worker_id": "ext-a"}, headers=_headers()).json()
    assert status["connected"] and status["ready"] == 1 and status["pending_items"] >= 1 and status["executor"] == "BROWSER_EXTENSION"
    assert status["tenant_id"] == harness.tenant_id


# ------------------------------------------------------------- matching


def test_normalize_url():
    assert _normalize_url("HTTPS://www.Jobs.Example.com/jobs/1/apply/?gh_src=x#top") == "jobs.example.com/jobs/1/apply"
    assert _normalize_url(None) == "" and _normalize_url("  ") == ""


def test_match_by_url_exact_prefix_and_never_other_tenant(client, harness, other_scheduler, other_tenant_id):
    attempt = harness.ready(company="Ext Match", application_url="https://boards.greenhouse.io/extmatch/jobs/123")
    harness.session.commit()
    # exact (with tracking noise), job page vs apply page (prefix either way)
    for url in ("https://boards.greenhouse.io/extmatch/jobs/123?gh_src=abc#app", "http://www.boards.greenhouse.io/extmatch/jobs/123/", "https://boards.greenhouse.io/extmatch/jobs/123/apply"):
        match = client.get("/api/v1/execution/extension/match", params={"url": url, "worker_id": "ext-a"}, headers=_headers()).json()
        assert match and match["application_id"] == attempt.id and match["attempt_status"] == "READY" and match["claimed_by_you"] is False, url
    assert client.get("/api/v1/execution/extension/match", params={"url": "https://boards.greenhouse.io/extmatch/jobs/1234", "worker_id": "ext-a"}, headers=_headers()).json() is None
    assert client.get("/api/v1/execution/extension/match", params={"url": "https://other.example/jobs/123", "worker_id": "ext-a"}, headers=_headers()).json() is None
    # Another tenant's extension never sees it.
    app.dependency_overrides[get_tenant_id] = lambda: other_tenant_id
    assert client.get("/api/v1/execution/extension/match", params={"url": "https://boards.greenhouse.io/extmatch/jobs/123", "worker_id": "ext-b"}, headers=_headers()).json() is None


def test_claim_is_guarded_between_workers_and_idempotent_for_the_owner(client, harness):
    attempt = harness.ready(company="Ext Claim", application_url="https://jobs.example.com/claim/apply")
    harness.session.commit()
    url = "https://jobs.example.com/claim/apply"
    first = client.post("/api/v1/execution/extension/claim", json={"url": url, "worker_id": "ext-a"}, headers=_headers()).json()
    assert first["item"]["state"] == "CLAIMED" and first["item"]["claimed_by"] == "ext-a" and first["claimed_by_you"]
    again = client.post("/api/v1/execution/extension/claim", json={"url": url, "worker_id": "ext-a"}, headers=_headers()).json()
    assert again["item"]["id"] == first["item"]["id"] and again["item"]["attempts"] == first["item"]["attempts"]
    other = client.post("/api/v1/execution/extension/claim", json={"url": url, "worker_id": "ext-b"}, headers=_headers())
    assert other.status_code == 200 and other.json() is None, "an item held by another live worker is invisible to a second extension"
    # The other worker cannot heartbeat or gate the owner's item either.
    assert client.post("/api/v1/execution/extension/heartbeat", json={"item_id": first["item"]["id"], "worker_id": "ext-b"}, headers=_headers()).status_code == 409
    beat = client.post("/api/v1/execution/extension/heartbeat", json={"item_id": first["item"]["id"], "worker_id": "ext-a"}, headers=_headers()).json()
    assert beat["lease_expires_at"] >= first["item"]["lease_expires_at"]
    assert client.post("/api/v1/execution/extension/claim", json={"url": "https://nowhere.example/x", "worker_id": "ext-a"}, headers=_headers()).json() is None
    harness.refresh(attempt)


# ------------------------------------------------------------ full flow


def _form(package):
    return {
        "source_url": package["target"]["canonical_url"],
        "executor_kind": "BROWSER_EXTENSION",
        "executor_version": "extension-test",
        "fields": [
            {"external_id": "email", "label": "Email *", "field_type": "email", "required": True, "selector": "#email", "input_type": "email"},
            {"external_id": "first", "label": "First name *", "field_type": "text", "required": True, "selector": "#first"},
            {"external_id": "cv", "label": "Resume/CV *", "field_type": "file", "required": True, "selector": "#resume", "accept": ".pdf"},
            {"external_id": "auth", "label": "Are you authorized to work in India?", "field_type": "select", "required": True, "options": [{"label": "Yes", "value": "1"}, {"label": "No", "value": "0"}], "selector": "#auth"},
        ],
        "metadata": {"title": "Apply", "custom_widgets": 0, "executor": "browser_extension"},
    }


def test_full_extension_flow_with_document_download_gate_and_verified_result(client, harness, db_session):
    attempt = harness.ready(company="Ext Flow", application_url="https://jobs.example.com/flow/apply")
    harness.session.commit()
    url = "https://jobs.example.com/flow/apply"
    claimed = client.post("/api/v1/execution/extension/claim", json={"url": url, "worker_id": "ext-a"}, headers=_headers()).json()
    item_id = claimed["item"]["id"]
    started = client.post(f"/api/v1/execution/items/{item_id}/start", json={"worker_id": "ext-a", "executor": "BROWSER_EXTENSION"}, headers=_headers()).json()
    assert started["outcome"] == "started" and started["run"]["executor_kind"] == "BROWSER_EXTENSION"
    package = started["package"]
    artifacts = package["execution_config"]["artifacts"]
    assert "RESUME" in artifacts and artifacts["RESUME"]["sha256"]
    # match now reports the item as ours
    match = client.get("/api/v1/execution/extension/match", params={"url": url, "worker_id": "ext-a"}, headers=_headers()).json()
    assert match["claimed_by_you"] is True and match["attempt_status"] == "SUBMITTING"

    mapped = client.post(f"/api/v1/execution/items/{item_id}/form", json={"worker_id": "ext-a", "form": _form(package)}, headers=_headers()).json()
    rows = mapped["snapshot"]["fields"]
    assert [r["position"] for r in rows] == [0, 1, 2, 3], "answers come back in the order the fields were sent"
    by_label = {r["label"]: r for r in rows}
    assert by_label["Resume/CV *"]["artifact_type"] == "RESUME" and by_label["Email *"]["answer"] == "ribhu@example.com" and by_label["First name *"]["answer"]
    assert by_label["Are you authorized to work in India?"]["selected_values"] == ["1"]

    # the document bytes, upload-eligible only, hash re-checked like the worker does
    download = client.get(f"/api/v1/documents/{artifacts['RESUME']['id']}/file", params={"for_upload": "true"}, headers=_headers())
    assert download.status_code == 200 and download.content[:4] == b"%PDF"
    digest = hashlib.sha256(download.content).hexdigest()
    assert download.headers["x-content-sha256"] == digest == artifacts["RESUME"]["sha256"]
    assert client.get(f"/api/v1/documents/{artifacts['RESUME']['id']}/file").status_code == 401

    # the pre-submit gate, then the result the page evidence produced
    gate = client.post("/api/v1/execution/extension/gate", json={"item_id": item_id, "worker_id": "ext-a"}, headers=_headers()).json()
    assert gate["ok"] is True and gate["failures"] == [] and gate["run_id"] == started["run"]["id"]
    run = client.get(f"/api/v1/execution/runs/{gate['run_id']}", headers=_headers()).json()
    assert run["submit_invoked"] is True and run["status"] == "RUNNING"
    second = client.post("/api/v1/execution/extension/gate", json={"item_id": item_id, "worker_id": "ext-a"}, headers=_headers()).json()
    assert second["ok"] is False and "SUBMIT_ALREADY_INVOKED" in second["failures"][0]

    result = {"outcome": "SUBMITTED", "submit_attempted": True, "application_url": "https://jobs.example.com/flow/confirmation", "confirmation_reference": "FLOW-77", "external_application_id": "FLOW-77", "message": "confirmation text observed after submit", "diagnostics": {"success_marker": "Thank you for applying", "fields_filled": 4, "uploads": [{"type": "RESUME", "sha256": digest}]}}
    reported = client.post(f"/api/v1/execution/items/{item_id}/result", json={"worker_id": "ext-a", "result": result}, headers=_headers()).json()
    assert reported["outcome"] == "SUBMITTED" and reported["attempt_status"] == "VERIFIED" and reported["queue_state"] == "SUCCEEDED"
    run = client.get(f"/api/v1/execution/runs/{gate['run_id']}", headers=_headers()).json()
    assert run["status"] == "VERIFIED" and run["verification_method"] == "application_id" and run["confirmation_reference"] == "FLOW-77"
    assert run["diagnostics"]["artifacts"]["RESUME"]["sha256"] == digest and run["diagnostics"]["uploads"][0]["sha256"] == digest
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.VERIFIED.value and attempt.external_application_id == "FLOW-77"


def test_submit_without_evidence_is_not_verified_and_unknown_is_uncertain(client, harness):
    a = harness.ready(company="Ext NoEvidence", application_url="https://jobs.example.com/noev/apply")
    b = harness.ready(company="Ext Unknown", application_url="https://jobs.example.com/unk/apply")
    harness.session.commit()

    def begin(url, worker):
        claimed = client.post("/api/v1/execution/extension/claim", json={"url": url, "worker_id": worker}, headers=_headers()).json()
        started = client.post(f"/api/v1/execution/items/{claimed['item']['id']}/start", json={"worker_id": worker, "executor": "BROWSER_EXTENSION"}, headers=_headers()).json()
        assert started["outcome"] == "started"
        assert client.post("/api/v1/execution/extension/gate", json={"item_id": claimed["item"]["id"], "worker_id": worker}, headers=_headers()).json()["ok"]
        return claimed["item"]["id"], started["run"]["id"]

    item_a, run_a = begin("https://jobs.example.com/noev/apply", "ext-a")
    reported = client.post(f"/api/v1/execution/items/{item_a}/result", json={"worker_id": "ext-a", "result": {"outcome": "SUBMITTED", "submit_attempted": True, "application_url": "https://jobs.example.com/noev/apply"}}, headers=_headers()).json()
    assert reported["attempt_status"] == "SUBMITTED", "a bare click claim is never VERIFIED"
    run = client.get(f"/api/v1/execution/runs/{run_a}", headers=_headers()).json()
    assert run["status"] == "SUBMITTED" and run["verification_status"] == "UNKNOWN"

    item_b, run_b = begin("https://jobs.example.com/unk/apply", "ext-a")
    reported = client.post(f"/api/v1/execution/items/{item_b}/result", json={"worker_id": "ext-a", "result": {"outcome": "UNKNOWN", "submit_attempted": True, "error_class": "TIMEOUT", "message": "tab closed after submit"}}, headers=_headers()).json()
    assert reported["attempt_status"] == "UNCERTAIN" and reported["queue_state"] == "NEEDS_REVIEW"
    harness.refresh(b)
    assert b.status == ApplicationStatus.UNCERTAIN.value
    # Only verification/confirmation settles it; no path re-runs the submit.
    confirmed = client.post(f"/api/v1/execution/runs/{run_b}/confirm", json={"submitted": True, "reference": "UNK-1"}, headers=_headers()).json()
    assert confirmed["status"] == "VERIFIED"
    harness.refresh(a)


def test_gate_refused_when_the_kill_switch_is_on_and_late_results_are_rejected(client, harness, db_session):
    from app.application.killswitch import set_paused

    harness.ready(company="Ext KillSwitch", application_url="https://jobs.example.com/ks/apply")
    harness.session.commit()
    claimed = client.post("/api/v1/execution/extension/claim", json={"url": "https://jobs.example.com/ks/apply", "worker_id": "ext-a"}, headers=_headers()).json()
    item_id = claimed["item"]["id"]
    assert client.post(f"/api/v1/execution/items/{item_id}/start", json={"worker_id": "ext-a", "executor": "BROWSER_EXTENSION"}, headers=_headers()).json()["outcome"] == "started"
    set_paused(db_session, True, "ext test")
    try:
        gate = client.post("/api/v1/execution/extension/gate", json={"item_id": item_id, "worker_id": "ext-a"}, headers=_headers()).json()
    finally:
        set_paused(db_session, False)
    assert gate["ok"] is False and gate["failures"][0].startswith("PAUSED")
    run = client.get(f"/api/v1/execution/runs/{gate['run_id']}", headers=_headers()).json()
    assert run["submit_invoked"] is False
    # the worker reports NEEDS_REVIEW (POLICY) like background.js does
    reported = client.post(f"/api/v1/execution/items/{item_id}/result", json={"worker_id": "ext-a", "result": {"outcome": "NEEDS_REVIEW", "submit_attempted": False, "error_class": "POLICY", "message": "pre-submit gate failed: " + gate["failures"][0], "diagnostics": {"gate_failures": gate["failures"]}}}, headers=_headers()).json()
    assert reported["attempt_status"] == "NEEDS_REVIEW"
    # gate/heartbeat/result on a settled item are refused
    assert client.post("/api/v1/execution/extension/gate", json={"item_id": item_id, "worker_id": "ext-a"}, headers=_headers()).status_code == 409
    assert client.post(f"/api/v1/execution/items/{item_id}/result", json={"worker_id": "ext-a", "result": {"outcome": "SUBMITTED"}}, headers=_headers()).status_code == 409


def test_handoff_from_the_extension_then_user_confirmation(client, harness):
    attempt = harness.ready(company="Ext Handoff", application_url="https://jobs.example.com/ho/apply")
    harness.session.commit()
    claimed = client.post("/api/v1/execution/extension/claim", json={"url": "https://jobs.example.com/ho/apply", "worker_id": "ext-a"}, headers=_headers()).json()
    item_id = claimed["item"]["id"]
    client.post(f"/api/v1/execution/items/{item_id}/start", json={"worker_id": "ext-a", "executor": "BROWSER_EXTENSION"}, headers=_headers())
    paused = client.post(f"/api/v1/execution/items/{item_id}/handoff", json={"worker_id": "ext-a", "reason": "MFA_REQUIRED", "message": "a one-time code prompt is on the page", "stopped_at": "mfa", "remaining_steps": ["enter the code yourself"]}, headers=_headers()).json()
    assert paused["outcome"] == "HANDOFF" and paused["attempt_status"] == "BLOCKED"
    confirmed = client.post(f"/api/v1/execution/runs/{paused['run_id']}/confirm", json={"submitted": False, "note": "did not apply"}, headers=_headers()).json()
    assert confirmed["status"] != "VERIFIED"
    harness.refresh(attempt)
    assert attempt.status != ApplicationStatus.VERIFIED.value


# ------------------------------------------------------ executor contract


def _target(url="https://jobs.example.com/x/apply", **kwargs):
    base = {"source": "CAREERS", "ats_family": ATSFamily.GENERIC_WEB, "canonical_url": url, "company": "X", "title": "Engineer", "method": ApplicationMethod.BROWSER_FORM}
    return ExecutionTarget(**{**base, **kwargs})


def _package(url="https://jobs.example.com/x/apply"):
    return ExecutionPackage(tenant_id="t", candidate_opportunity_id="co", opportunity_id="o", preparation_id="p", application_id="a", attempt_number=1, preparation_version=1, preparation_fingerprint="f", target=_target(url))


def test_browser_extension_executor_contract_and_conservative_verify():
    executor = BrowserExtensionExecutor()
    assert executor.kind is ExecutorKind.BROWSER_EXTENSION
    assert executor.can_handle(_package().target)
    assert not executor.can_handle(_target("mailto:jobs@example.com", ats_family=ATSFamily.MANUAL, method=ApplicationMethod.MANUAL))
    assert not executor.can_handle(_target("javascript:void(0)"))
    with pytest.raises(Exception):
        executor.prepare(_package())
    with pytest.raises(Exception):
        executor.execute(_package(), None, [])
    pkg = _package()
    v = executor.verify(pkg, ExecutionResult(outcome=ExecutionOutcome.SUBMITTED, submit_attempted=True, confirmation_reference="R-1"))
    assert v.status is VerificationStatus.VERIFIED and v.method is VerificationMethod.APPLICATION_ID
    v = executor.verify(pkg, ExecutionResult(outcome=ExecutionOutcome.SUBMITTED, submit_attempted=True, diagnostics={"success_marker": "Thank you for applying"}))
    assert v.status is VerificationStatus.VERIFIED and v.method is VerificationMethod.CONFIRMATION_TEXT
    v = executor.verify(pkg, ExecutionResult(outcome=ExecutionOutcome.SUBMITTED, submit_attempted=True, application_url="https://jobs.example.com/x/thanks"))
    assert v.status is VerificationStatus.LIKELY and v.method is VerificationMethod.REDIRECT_URL
    v = executor.verify(pkg, ExecutionResult(outcome=ExecutionOutcome.SUBMITTED, submit_attempted=True, application_url="https://jobs.example.com/x/apply"))
    assert v.status is VerificationStatus.UNKNOWN
    v = executor.verify(pkg, ExecutionResult(outcome=ExecutionOutcome.UNKNOWN, submit_attempted=True))
    assert v.status is VerificationStatus.UNKNOWN


def test_default_registry_includes_the_extension_executor():
    from app.execution.service import default_registry

    assert ExecutorKind.BROWSER_EXTENSION in default_registry()
