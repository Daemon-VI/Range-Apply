"""Scheduler API and dashboard: preview is read-only, run/status/history/capacity/queue."""

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
from app.scheduler.database.models import SchedulerRunRow
from tests.conftest import AUTH_HEADERS
from tests.pipeline.conftest import JobFactory, MatchFactory
from tests.preparation.conftest import FACT_ANSWERS, OpportunityFactory

TENANT = f"api-p5-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module", autouse=True)
def _tenant():
    session = get_session_factory()()
    session.add(TenantRow(id=TENANT, name="api p5"))
    session.commit()
    repo = EvidenceRepository(session, TENANT)
    SeedImporter(repo).import_file(settings.career_data_path)
    for category, question, answer in FACT_ANSWERS:
        repo.create_answer(AnswerBankEntryCreate(category=category, question=question, answer=answer, status=AnswerStatus.APPROVED), "test")
    repo.commit()
    previous = app.dependency_overrides.get(get_tenant_id)
    app.dependency_overrides[get_tenant_id] = lambda: TENANT
    jobs = JobFactory(session)
    matches = MatchFactory(session)
    factory = OpportunityFactory(session, TENANT, jobs, matches)
    good = factory.make(title="Backend Engineer", fit_score=85, company="Alpha")
    bad = factory.make(title="Support Engineer", fit_score=25, eligibility="INELIGIBLE", company="Beta")
    yield {"good": good.id, "bad": bad.id}
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


def test_preview_status_run_history_capacity_queue(client, _tenant):
    preview = client.get("/api/v1/scheduler/preview").json()
    assert preview["considered"] == 2 and preview["would_admit"] == 1
    codes = {d["candidate_opportunity_id"]: d["code"] for d in preview["decisions"]}
    assert codes == {_tenant["good"]: "ADMITTED", _tenant["bad"]: "INELIGIBLE"}
    assert preview["capacity"]["day"]["used"] == 0
    session = get_session_factory()()
    try:
        assert session.query(SchedulerRunRow).filter_by(tenant_id=TENANT).count() == 0, "preview writes nothing"
    finally:
        session.close()

    assert client.post("/api/v1/scheduler/run", json={}).status_code == 401
    resp = client.post("/api/v1/scheduler/run", json={"trigger": "api-test", "window": 100, "prepare": True}, headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.text
    run = resp.json()
    assert run["status"] == "COMPLETED" and run["admitted"] == 1 and run["blocked_by_reason"] == {"INELIGIBLE": 1}
    assert run["preparation"]["READY_FOR_EXECUTION"] == 1 and run["ready_for_execution"] == 1

    status = client.get("/api/v1/scheduler/status").json()
    assert status["day"]["used"] == 1 and status["day"]["remaining"] == status["day"]["cap"] - 1
    assert status["week"]["used"] == 1 and status["cooldowns_active"] == 1
    assert status["ready_for_execution"] == 1 and status["last_run"]["id"] == run["id"]
    assert status["queue_by_state"]["SUCCEEDED"] == 1
    assert client.get("/api/v1/scheduler/capacity").json()["tenant_id"] == TENANT

    history = client.get("/api/v1/scheduler/runs").json()
    assert history["total"] == 1 and history["items"][0]["id"] == run["id"]
    assert client.get(f"/api/v1/scheduler/runs/{run['id']}").json()["trigger"] == "api-test"
    assert client.get("/api/v1/scheduler/runs/nope").status_code == 404

    queue = client.get("/api/v1/scheduler/queue").json()
    # Phase 6: the READY attempt also received its SUBMIT item.
    assert queue["by_action"] == {"PREPARE": {"SUCCEEDED": 1}, "SUBMIT": {"PENDING": 1}} and queue["ready_for_execution"] == 1
    assert run["execution_enqueued"] == 1

    again = client.post("/api/v1/scheduler/run", json={}, headers=AUTH_HEADERS).json()
    assert again["admitted"] == 0 and again["already_queued"] == 1


def test_policy_accepts_new_fields(client, _tenant):
    resp = client.put("/api/v1/policy/", json={"minimum_fit_score": 30, "timezone": "Asia/Kolkata"}, headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.text
    assert resp.json()["minimum_fit_score"] == 30 and resp.json()["timezone"] == "Asia/Kolkata"
    bad = client.put("/api/v1/policy/", json={"timezone": "Nowhere/Land"}, headers=AUTH_HEADERS)
    assert bad.status_code == 422


def test_dashboard_pages(client, _tenant):
    key = AUTH_HEADERS["X-API-Key"]
    page = client.get("/dashboard/scheduler", params={"key": key})
    assert page.status_code == 200
    assert "Run history" in page.text and "INELIGIBLE" in page.text and "Company cool-downs" in page.text
    run_id = client.get("/api/v1/scheduler/runs").json()["items"][0]["id"]
    detail = client.get(f"/dashboard/scheduler/runs/{run_id}")
    assert detail.status_code == 200 and "Blocked by reason" in detail.text
    posted = client.post("/dashboard/scheduler/run", data={"window": "50"})
    assert posted.status_code == 303 and "run:COMPLETED" in posted.headers["location"]
