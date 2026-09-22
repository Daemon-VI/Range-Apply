"""/api/v1/opportunities, /api/v1/policy, /api/v1/queue over a private tenant."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.career.database.models import TenantRow
from app.database import get_session_factory
from app.main import app
from tests.conftest import AUTH_HEADERS
from tests.pipeline.conftest import JobFactory, MatchFactory

TENANT = f"api-p2-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module", autouse=True)
def _tenant_override():
    session = get_session_factory()()
    session.add(TenantRow(id=TENANT, name="api tenant"))
    session.commit()
    previous = app.dependency_overrides.get(get_tenant_id)
    app.dependency_overrides[get_tenant_id] = lambda: TENANT
    yield
    if previous is not None:
        app.dependency_overrides[get_tenant_id] = previous
    else:
        app.dependency_overrides.pop(get_tenant_id, None)
    row = session.get(TenantRow, TENANT)
    if row is not None:
        session.delete(row)
        session.commit()
    session.close()


@pytest.fixture(scope="module")
def seeded():
    session = get_session_factory()()
    jobs = JobFactory(session)
    matches = MatchFactory(session)
    strong = jobs.make(title="Backend Engineer", posted_days_ago=1)
    weak = jobs.make(title="Sales Engineer", posted_days_ago=30)
    # Ineligible but high fit and older: priority still ranks it (order only),
    # admission does not. Older so the fresh eligible job sorts first.
    blocked = jobs.make(title="Staff Engineer", posted_days_ago=20)
    matches.make(strong, 85, "ELIGIBLE")
    matches.make(weak, 35, "ELIGIBLE")
    matches.make(blocked, 90, "INELIGIBLE", blocking_reasons=["Graduation year: requires 2025"])
    yield {"run_id": matches.run.id, "strong": strong, "weak": weak, "blocked": blocked}
    matches.cleanup()
    jobs.cleanup()
    session.close()


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def test_policy_defaults_and_update(client):
    assert client.get("/api/v1/policy/").json()["enabled_bands"] == ["HIGH", "MEDIUM", "LOW"]
    assert client.put("/api/v1/policy/", json={"daily_cap": 5}).status_code == 401
    resp = client.put("/api/v1/policy/", json={"daily_cap": 75, "lane_by_band": {"HIGH": "AUTO", "MEDIUM": "REVIEW", "LOW": "REVIEW"}}, headers=AUTH_HEADERS)
    assert resp.status_code == 200 and resp.json()["daily_cap"] == 75 and resp.json()["version"] == 2
    bad = client.put("/api/v1/policy/", json={"band_thresholds": {"HIGH": 10, "MEDIUM": 50}}, headers=AUTH_HEADERS)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "validation_failed"


def test_sync_list_and_detail(client, seeded):
    assert client.post("/api/v1/opportunities/sync", json={}).status_code == 401
    resp = client.post("/api/v1/opportunities/sync", json={"run_id": seeded["run_id"]}, headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["synced"] == 3 and report["admitted"] == 2 and report["not_admitted"] == 1

    listing = client.get("/api/v1/opportunities/", params={"limit": 10}).json()
    assert listing["total"] == 3
    assert [i["title"] for i in listing["items"]][0] == "Backend Engineer", "highest priority first"
    assert all("priority_score" in i["candidate"] for i in listing["items"])
    assert client.get("/api/v1/opportunities/", params={"eligibility": "INELIGIBLE"}).json()["total"] == 1
    assert client.get("/api/v1/opportunities/", params={"fit_band": "LOW"}).json()["total"] == 1
    assert client.get("/api/v1/opportunities/", params={"admitted": "true"}).json()["total"] == 2

    summary = client.get("/api/v1/opportunities/summary").json()
    assert summary["by_state"]["ELIGIBLE"] == 2 and summary["by_state"]["INELIGIBLE"] == 1
    assert summary["queue"]["PENDING"] == 0

    co_id = listing["items"][0]["candidate"]["id"]
    detail = client.get(f"/api/v1/opportunities/{co_id}").json()
    assert detail["fit"]["fit_score"] == 85 and detail["fit"]["fit_band"] == "HIGH"
    assert detail["eligibility"]["decision"] == "ELIGIBLE"
    assert detail["priority"]["weights_version"] == "priority-v1"
    assert detail["job_ids"] == [seeded["strong"].id]
    assert client.get(f"/api/v1/opportunities/{co_id}/eligibility").json()[0]["decision"] == "ELIGIBLE"
    assert client.get("/api/v1/opportunities/does-not-exist").status_code == 404


def test_enqueue_respects_policy_and_queue_lifecycle_via_api(client, seeded):
    items = client.get("/api/v1/opportunities/", params={"limit": 10}).json()["items"]
    by_title = {i["title"]: i["candidate"] for i in items}
    strong, blocked = by_title["Backend Engineer"], by_title["Staff Engineer"]

    refused = client.post(f"/api/v1/opportunities/{blocked['id']}/enqueue", json={"action": "SUBMIT"}, headers=AUTH_HEADERS)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "policy_blocked"

    created = client.post(f"/api/v1/opportunities/{strong['id']}/enqueue", json={"action": "SUBMIT"}, headers=AUTH_HEADERS)
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["lane"] == "AUTO" and item["state"] == "PENDING" and item["priority"] == strong["priority_score"]
    again = client.post(f"/api/v1/opportunities/{strong['id']}/enqueue", json={"action": "SUBMIT"}, headers=AUTH_HEADERS).json()
    assert again["id"] == item["id"], "idempotent"
    assert client.get(f"/api/v1/opportunities/{strong['id']}").json()["candidate"]["state"] == "QUEUED"

    claimed = client.post("/api/v1/queue/claim", json={"worker_id": "ext-1", "action": "SUBMIT"}, headers=AUTH_HEADERS).json()
    assert [c["id"] for c in claimed] == [item["id"]]
    assert client.post(f"/api/v1/queue/{item['id']}/start", json={"worker_id": "other"}, headers=AUTH_HEADERS).status_code == 409
    assert client.post(f"/api/v1/queue/{item['id']}/start", json={"worker_id": "ext-1"}, headers=AUTH_HEADERS).json()["state"] == "PROCESSING"
    done = client.post(f"/api/v1/queue/{item['id']}/succeed", json={"worker_id": "ext-1", "result": {"confirmation": "X1"}}, headers=AUTH_HEADERS).json()
    assert done["state"] == "SUCCEEDED" and done["result"] == {"confirmation": "X1"}

    dup = client.post(f"/api/v1/opportunities/{strong['id']}/enqueue", json={"action": "SUBMIT"}, headers=AUTH_HEADERS)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "conflict"

    assert client.get("/api/v1/queue/summary").json()["SUCCEEDED"] == 1
    assert client.get("/api/v1/queue/", params={"state": "SUCCEEDED"}).json()["total"] == 1
    assert client.post("/api/v1/queue/claim", json={"worker_id": "x"}).status_code == 401


def test_state_change_endpoint(client, seeded):
    items = client.get("/api/v1/opportunities/", params={"fit_band": "LOW"}).json()["items"]
    co_id = items[0]["candidate"]["id"]
    resp = client.post(f"/api/v1/opportunities/{co_id}/state", json={"state": "SKIPPED", "reason": "not now"}, headers=AUTH_HEADERS)
    assert resp.status_code == 200 and resp.json()["state"] == "SKIPPED" and resp.json()["skipped_reason"] == "not now"
    illegal = client.post(f"/api/v1/opportunities/{co_id}/state", json={"state": "OFFER"}, headers=AUTH_HEADERS)
    assert illegal.status_code == 409
