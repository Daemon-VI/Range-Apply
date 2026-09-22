"""/api/v1/career: writes persist, are audited, and require the API key.

The tenant dependency is overridden with a fresh tenant per module so these
writes never touch the default tenant the rest of the suite reads."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.career.database.models import TenantRow
from app.database import get_session_factory
from app.main import app
from tests.conftest import AUTH_HEADERS

TENANT = f"api-{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="module", autouse=True)
def _tenant_override():
    previous = app.dependency_overrides.get(get_tenant_id)
    app.dependency_overrides[get_tenant_id] = lambda: TENANT
    yield
    if previous is not None:
        app.dependency_overrides[get_tenant_id] = previous
    else:
        app.dependency_overrides.pop(get_tenant_id, None)
    session = get_session_factory()()
    try:
        row = session.get(TenantRow, TENANT)
        if row is not None:
            session.delete(row)
            session.commit()
    finally:
        session.close()


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def test_reads_bootstrap_the_tenant_from_seed(client):
    resp = client.get("/profile")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Ribhu Siripurapu"
    resp = client.get("/api/v1/career/evidence", params={"kind": "SKILL"})
    assert resp.status_code == 200
    keys = {n["key"] for n in resp.json()}
    assert "skill-python" in keys
    assert resp.json()[0]["grade"] in {"CONFIRMED", "UNVERIFIED", "NEEDS_REVIEW", "APPROXIMATE"}


def test_writes_require_api_key(client):
    body = {"kind": "FACT", "label": "x", "claim": "x"}
    assert client.post("/api/v1/career/evidence", json=body).status_code == 401
    assert client.put("/api/v1/career/profile", json={"email": "a@b.c"}).status_code == 401


def test_evidence_lifecycle_is_persisted_and_audited(client):
    created = client.post(
        "/api/v1/career/evidence",
        json={
            "key": "fact-api-test",
            "kind": "FACT",
            "label": "API test fact",
            "claim": "Created through the API",
            "verification_status": "NEEDS_REVIEW",
            "source_type": "CANDIDATE_ENTERED",
            "source_ref": "test",
        },
        headers=AUTH_HEADERS,
    )
    assert created.status_code == 201, created.text
    assert created.json()["grade"] == "NEEDS_REVIEW"

    dup = client.post(
        "/api/v1/career/evidence",
        json={"key": "fact-api-test", "kind": "FACT", "label": "x", "claim": "x"},
        headers=AUTH_HEADERS,
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "conflict"

    patched = client.patch(
        "/api/v1/career/evidence/fact-api-test",
        json={"verification_status": "VERIFIED"},
        headers=AUTH_HEADERS,
    )
    assert patched.status_code == 200
    assert patched.json()["grade"] == "CONFIRMED"
    assert patched.json()["verified_by"] == "api"
    assert patched.json()["version"] == 2

    history = client.get("/api/v1/career/evidence/fact-api-test/history").json()
    assert [e["action"] for e in history] == ["updated", "created"]
    assert history[0]["before"]["verification_status"] == "NEEDS_REVIEW"

    removed = client.delete(
        "/api/v1/career/evidence/fact-api-test", params={"reason": "test"}, headers=AUTH_HEADERS
    )
    assert removed.status_code == 200 and removed.json()["grade"] == "REMOVED"
    assert "fact-api-test" not in {n["key"] for n in client.get("/api/v1/career/evidence").json()}
    assert "fact-api-test" in {
        n["key"]
        for n in client.get("/api/v1/career/evidence", params={"include_removed": "true"}).json()
    }
    restored = client.post("/api/v1/career/evidence/fact-api-test/restore", headers=AUTH_HEADERS)
    assert restored.json()["status"] == "ACTIVE"


def test_extracted_evidence_cannot_be_created_verified(client):
    resp = client.post(
        "/api/v1/career/evidence",
        json={
            "kind": "SKILL",
            "label": "Zzz Extracted Skill",
            "claim": "Extracted by a model",
            "verification_status": "VERIFIED",
            "source_type": "LLM_EXTRACTED",
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


def test_relationship_endpoints(client):
    body = {"from_key": "careeros", "to_key": "skill-fastapi", "relation": "DEMONSTRATES"}
    first = client.post("/api/v1/career/relationships", json=body, headers=AUTH_HEADERS)
    assert first.status_code == 201
    rels = client.get("/api/v1/career/evidence/careeros/relationships").json()
    assert any(r["to_key"] == "skill-fastapi" and r["relation"] == "DEMONSTRATES" for r in rels)
    gone = client.request("DELETE", "/api/v1/career/relationships", json=body, headers=AUTH_HEADERS)
    assert gone.json() == {"deleted": True}
    bad = client.post(
        "/api/v1/career/relationships",
        json={"from_key": "careeros", "to_key": "nope", "relation": "ABOUT"},
        headers=AUTH_HEADERS,
    )
    assert bad.status_code == 404


def test_profile_and_preferences_persist(client):
    resp = client.put(
        "/api/v1/career/profile",
        json={"email": "ribhu@example.com", "work_authorization": "Indian citizen"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert client.get("/profile").json()["email"] == "ribhu@example.com"
    assert client.get("/api/v1/career/profile").json()["work_authorization"] == "Indian citizen"

    prefs = client.get("/preferences").json()
    prefs["preferred_locations"] = ["Hyderabad", "Remote"]
    resp = client.put("/api/v1/career/preferences", json=prefs, headers=AUTH_HEADERS)
    assert resp.status_code == 200
    assert client.get("/preferences").json()["preferred_locations"] == ["Hyderabad", "Remote"]
    audit = client.get("/api/v1/career/audit", params={"entity_type": "candidate_profile"}).json()
    assert audit and audit[0]["action"] == "updated"


def test_positioning_and_answers_endpoints(client):
    created = client.post(
        "/api/v1/career/positioning",
        json={
            "role_family": "ML Engineer",
            "headline": "ML-leaning engineering student",
            "summary": "TensorFlow, LSTMs, computer vision projects.",
            "evidence": [
                {"key": "skill-tensorflow", "position": 0},
                {"key": "plant-disease", "position": 1, "section": "projects"},
            ],
        },
        headers=AUTH_HEADERS,
    )
    assert created.status_code == 201, created.text
    variant_id = created.json()["id"]
    assert [e["key"] for e in created.json()["evidence"]] == ["skill-tensorflow", "plant-disease"]

    bad = client.post(
        "/api/v1/career/positioning",
        json={"role_family": "X", "evidence": [{"key": "does-not-exist"}]},
        headers=AUTH_HEADERS,
    )
    assert bad.status_code == 422

    patched = client.patch(
        f"/api/v1/career/positioning/{variant_id}", json={"is_active": False}, headers=AUTH_HEADERS
    )
    assert patched.json()["is_active"] is False and patched.json()["version"] == 2
    assert client.get("/api/v1/career/positioning", params={"active_only": "true"}).json() == []

    answer = client.post(
        "/api/v1/career/answers",
        json={
            "category": "notice_period",
            "question": "What is your notice period?",
            "answer": "Available immediately.",
        },
        headers=AUTH_HEADERS,
    )
    assert answer.status_code == 201
    entry_id = answer.json()["id"]
    assert client.get("/api/v1/career/answers/lookup", params={"question": "what is your notice period"}).json() is None
    approved = client.post(f"/api/v1/career/answers/{entry_id}/approve", headers=AUTH_HEADERS)
    assert approved.json()["status"] == "APPROVED"
    found = client.get("/api/v1/career/answers/lookup", params={"question": "What is your NOTICE period?"}).json()
    assert found["id"] == entry_id and found["answer"] == "Available immediately."

    assert client.delete(f"/api/v1/career/positioning/{variant_id}", headers=AUTH_HEADERS).json() == {"deleted": True}
    assert client.delete(f"/api/v1/career/answers/{entry_id}", headers=AUTH_HEADERS).json() == {"deleted": True}


def test_import_endpoint_is_idempotent(client):
    resp = client.post("/api/v1/career/import", json={}, headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["created"] == [] and report["updated"] == []
    assert len(report["skipped"]) > 20
