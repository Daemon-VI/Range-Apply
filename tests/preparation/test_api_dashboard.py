"""/api/v1/preparations and /dashboard/preparations over a private tenant."""

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
from tests.conftest import AUTH_HEADERS
from tests.pipeline.conftest import JobFactory, MatchFactory
from tests.preparation.conftest import FACT_ANSWERS, OpportunityFactory

TENANT = f"api-p4-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module", autouse=True)
def _tenant():
    session = get_session_factory()()
    session.add(TenantRow(id=TENANT, name="api p4"))
    session.commit()
    repo = EvidenceRepository(session, TENANT)
    SeedImporter(repo).import_file(settings.career_data_path)
    for category, question, answer in FACT_ANSWERS[:3]:  # salary left unanswered on purpose
        repo.create_answer(AnswerBankEntryCreate(category=category, question=question, answer=answer, status=AnswerStatus.APPROVED), "test")
    repo.commit()
    previous = app.dependency_overrides.get(get_tenant_id)
    app.dependency_overrides[get_tenant_id] = lambda: TENANT
    jobs = JobFactory(session)
    matches = MatchFactory(session)
    factory = OpportunityFactory(session, TENANT, jobs, matches)
    co = factory.make(fit_score=85)
    low = factory.make(title="Support Engineer", fit_score=25)
    yield {"co": co.id, "low": low.id}
    if previous is not None:
        app.dependency_overrides[get_tenant_id] = previous
    else:
        app.dependency_overrides.pop(get_tenant_id, None)
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


def test_prepare_inspect_answer_approve(client, _tenant):
    assert client.post("/api/v1/preparations/", json={"candidate_opportunity_id": _tenant["co"]}).status_code == 401
    resp = client.post("/api/v1/preparations/", json={"candidate_opportunity_id": _tenant["co"]}, headers=AUTH_HEADERS)
    assert resp.status_code == 201, resp.text
    prep = resp.json()
    assert prep["tailoring_level"] == "L2" and prep["status"] == "NEEDS_USER_INPUT" and prep["ai_calls"] == 0
    assert {a["artifact_type"] for a in prep["artifacts"]} == {"RESUME", "COVER_LETTER"}
    salary = next(a for a in prep["answers"] if a["category"] == "salary")
    assert salary["status"] == "NEEDS_USER_INPUT"

    trace = client.get(f"/api/v1/preparations/{prep['id']}/evidence").json()
    assert "skill-python" in trace["evidence"] and trace["evidence"]["skill-python"]["verification_status"] == "VERIFIED"
    assert all(b["evidence_keys"] for b in trace["blocks"] if b["kind"] == "CLAIM")

    again = client.post("/api/v1/preparations/", json={"candidate_opportunity_id": _tenant["co"]}, headers=AUTH_HEADERS).json()
    assert again["id"] == prep["id"], "idempotent"

    answered = client.put(
        f"/api/v1/preparations/{prep['id']}/answers/{salary['id']}",
        json={"answer": "Open to a competitive package.", "save_to_bank": True},
        headers=AUTH_HEADERS,
    ).json()
    assert answered["status"] == "READY"
    assert client.get("/api/v1/preparations/summary").json()["by_status"]["READY"] >= 1
    listing = client.get("/api/v1/preparations/", params={"status": "READY"}).json()
    assert listing["total"] >= 1
    assert client.get(f"/api/v1/preparations/by-opportunity/{_tenant['co']}").json()[0]["version"] == 1

    rejected = client.post(f"/api/v1/preparations/{prep['id']}/reject", json={"reason": "tone"}, headers=AUTH_HEADERS).json()
    assert rejected["status"] == "NEEDS_REVIEW"
    approved = client.post(f"/api/v1/preparations/{prep['id']}/approve", headers=AUTH_HEADERS).json()
    assert approved["status"] == "READY" and approved["approved_by"] == "api"
    assert client.get("/api/v1/preparations/nope").status_code == 404


def test_batch_and_dashboard(client, _tenant):
    report = client.post(
        "/api/v1/preparations/batch",
        json={"candidate_opportunity_ids": [_tenant["co"], _tenant["low"], "missing"], "tailoring_level": "L0"},
        headers=AUTH_HEADERS,
    ).json()
    assert report["requested"] == 3 and report["created"] >= 1 and report["ai_calls"] == 0
    assert any("missing" in e for e in report["errors"])

    key = AUTH_HEADERS["X-API-Key"]
    page = client.get("/dashboard/preparations", params={"key": key})
    assert page.status_code == 200 and "Application packages" in page.text
    prep_id = client.get(f"/api/v1/preparations/by-opportunity/{_tenant['low']}").json()[0]["id"]
    detail = client.get(f"/dashboard/preparations/{prep_id}")
    assert detail.status_code == 200 and "Why it is here" in detail.text and "skill-python" in detail.text
    regen = client.post(f"/dashboard/preparations/{prep_id}/regenerate", data={"tailoring_level": "L1"})
    assert regen.status_code == 303 and "regenerated" in regen.headers["location"]
    new_id = regen.headers["location"].split("/")[-1].split("?")[0]
    assert client.get(f"/api/v1/preparations/{new_id}").json()["tailoring_level"] == "L1"
