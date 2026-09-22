"""/api/v1/execution and /dashboard/execution over a private tenant.

Exercises the external-executor flow (claim -> start -> form -> result ->
verify/confirm) and the in-process MOCK flow (run-queue), plus the pages.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.career.database.models import TenantRow
from app.career.importer import SeedImporter
from app.career.models import AnswerBankEntryCreate, AnswerStatus
from app.career.repository import EvidenceRepository
from app.config import settings
from app.database import get_session_factory
from app.main import app
from app.scheduler.service import SchedulerService
from tests.conftest import AUTH_HEADERS
from tests.pipeline.conftest import JobFactory, MatchFactory
from tests.preparation.conftest import FACT_ANSWERS, OpportunityFactory

TENANT = f"api-p6-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module", autouse=True)
def _tenant():
    session = get_session_factory()()
    session.add(TenantRow(id=TENANT, name="api p6"))
    session.commit()
    repo = EvidenceRepository(session, TENANT)
    SeedImporter(repo).import_file(settings.career_data_path)
    for category, question, answer in FACT_ANSWERS:
        repo.create_answer(AnswerBankEntryCreate(category=category, question=question, answer=answer, status=AnswerStatus.APPROVED), "test")
    repo.upsert_profile({"email": "ribhu@example.com"}, None, "test")
    repo.commit()
    previous = app.dependency_overrides.get(get_tenant_id)
    app.dependency_overrides[get_tenant_id] = lambda: TENANT
    jobs = JobFactory(session)
    matches = MatchFactory(session)
    factory = OpportunityFactory(session, TENANT, jobs, matches)
    a = factory.make(title="Backend Engineer", fit_score=85, company="Alpha")
    b = factory.make(title="Data Engineer", fit_score=80, company="Beta")
    run = SchedulerService(session, TENANT, actor="test").run(prepare=True, prepare_limit=10)
    assert run.ready_for_execution == 2 and run.execution_enqueued == 2, (run.blocked_by_reason, run.errors)
    from app.scheduler.attempts import AttemptRepository

    repo_a = AttemptRepository(session, TENANT)
    yield {"a": repo_a.get_for_opportunity(a.opportunity_id).id, "b": repo_a.get_for_opportunity(b.opportunity_id).id}
    if previous is not None:
        app.dependency_overrides[get_tenant_id] = previous
    else:
        app.dependency_overrides.pop(get_tenant_id, None)
    session.rollback()
    matches.cleanup()
    jobs.cleanup()
    row = session.get(TenantRow, TENANT)
    if row is not None:
        session.delete(row)
        session.commit()
    session.close()


@pytest.fixture(scope="module")
def client():
    return TestClient(app, follow_redirects=False)


def test_external_executor_flow(client, _tenant):
    ready = client.get("/api/v1/execution/ready").json()
    assert ready["total"] == 2 and {r["status"] for r in ready["items"]} == {"READY"}
    preview = client.get(f"/api/v1/execution/attempts/{_tenant['a']}/preview").json()
    assert preview["preconditions"]["ok"] and preview["package"]["application_id"] == _tenant["a"]
    assert preview["package"]["resume"]["artifact_type"] == "RESUME" and preview["package"]["target"]["ats_family"] == "greenhouse"
    assert client.post("/api/v1/execution/claim", json={"worker_id": "ext"}).status_code == 401

    claimed = client.post("/api/v1/execution/claim", json={"worker_id": "ext", "limit": 1}, headers=AUTH_HEADERS).json()
    assert len(claimed) == 1 and claimed[0]["item"]["state"] == "CLAIMED"
    item_id = claimed[0]["item"]["id"]
    application_id = claimed[0]["application_id"]
    started = client.post(f"/api/v1/execution/items/{item_id}/start", json={"worker_id": "ext", "executor": "MANUAL"}, headers=AUTH_HEADERS).json()
    assert started["outcome"] == "started" and started["run"]["status"] == "RUNNING" and started["package"]["application_id"] == application_id
    # The executor reports the form it found; the server maps answers.
    form = {
        "source_url": started["package"]["target"]["canonical_url"],
        "executor_kind": "BROWSER_EXTENSION",
        "executor_version": "ext-0.1",
        "fields": [
            {"external_id": "email", "label": "Email", "field_type": "text", "required": True},
            {"external_id": "cv", "label": "Resume", "field_type": "file", "required": True},
            {"external_id": "auth", "label": "Are you authorized to work in India?", "field_type": "select", "required": True, "options": [{"label": "Yes"}, {"label": "No"}]},
            {"external_id": "eeo", "label": "Gender", "field_type": "select", "required": False, "options": [{"label": "Male"}, {"label": "Female"}]},
        ],
    }
    mapped = client.post(f"/api/v1/execution/items/{item_id}/form", json={"worker_id": "ext", "form": form}, headers=AUTH_HEADERS).json()
    assert mapped["blocking_fields"] == 0
    statuses = {f["label"]: (f["status"], f["source"]) for f in mapped["snapshot"]["fields"]}
    assert statuses["Email"] == ("ANSWERED", "PROFILE") and statuses["Resume"] == ("ANSWERED", "ARTIFACT")
    assert statuses["Are you authorized to work in India?"][0] == "ANSWERED" and statuses["Are you authorized to work in India?"][1] in ("PREPARATION", "ANSWER_BANK")
    assert statuses["Gender"] == ("SKIPPED", "NONE")
    # A CAPTCHA appears: pause, never bypass.
    paused = client.post(f"/api/v1/execution/items/{item_id}/handoff", json={"worker_id": "ext", "reason": "CAPTCHA_REQUIRED", "message": "hCaptcha shown", "stopped_at": "captcha", "remaining_steps": ["solve", "submit"]}, headers=AUTH_HEADERS).json()
    assert paused["outcome"] == "HANDOFF" and paused["attempt_status"] == "BLOCKED"
    run_id = paused["run_id"]
    run = client.get(f"/api/v1/execution/runs/{run_id}").json()
    assert run["handoff_reason"] == "CAPTCHA_REQUIRED" and run["handoff"]["remaining_steps"] == ["solve", "submit"]
    # The candidate finishes in the browser and confirms.
    confirmed = client.post(f"/api/v1/execution/runs/{run_id}/confirm", json={"submitted": True, "reference": "REF-1"}, headers=AUTH_HEADERS).json()
    assert confirmed["status"] == "VERIFIED" and confirmed["verification_method"] == "user_confirmation" and confirmed["confirmation_reference"] == "REF-1"
    attempts = client.get("/api/v1/execution/attempts", params={"status": "VERIFIED"}).json()
    assert [a["id"] for a in attempts["items"]] == [application_id]
    summary = client.get("/api/v1/execution/summary").json()
    assert summary["metrics"]["captcha_handoffs"] == 1 and summary["metrics"]["verified"] == 1
    # No client can set a status directly; a bare result on an unowned item is refused.
    late = client.post(f"/api/v1/execution/items/{item_id}/result", json={"worker_id": "ext", "result": {"outcome": "SUBMITTED"}}, headers=AUTH_HEADERS)
    assert late.status_code == 409


def test_mock_run_queue_and_dashboard(client, _tenant, monkeypatch):
    counts = client.post("/api/v1/execution/run-queue", json={"worker_id": "local", "limit": 5, "executor": "MOCK"}, headers=AUTH_HEADERS).json()
    assert counts["claimed"] == 1 and counts["submitted"] == 1
    # The first test claimed whichever attempt ranked first; this one drained the other.
    by_attempt = {aid: client.get(f"/api/v1/execution/attempts/{aid}/runs").json() for aid in (_tenant["a"], _tenant["b"])}
    mock_id, mock_runs = next((aid, runs) for aid, runs in by_attempt.items() if runs[0]["executor_kind"] == "MOCK")
    assert mock_runs[0]["status"] == "VERIFIED" and mock_runs[0]["submit_invoked"]
    assert client.post(f"/api/v1/execution/attempts/{mock_id}/retry", json={}, headers=AUTH_HEADERS).status_code == 409
    key = AUTH_HEADERS["X-API-Key"]
    page = client.get("/dashboard/execution", params={"key": key})
    assert page.status_code == 200 and "Execution metrics" in page.text and "VERIFIED" in page.text
    manual_id = next(aid for aid in (_tenant["a"], _tenant["b"]) if aid != mock_id)
    detail = client.get(f"/dashboard/execution/attempts/{manual_id}")
    assert detail.status_code == 200 and "CAPTCHA_REQUIRED" in detail.text and "Execution runs" in detail.text
    assert client.get("/api/v1/execution/runs/nope").status_code == 404
