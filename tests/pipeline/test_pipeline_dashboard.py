"""Inspection pages render behind dashboard auth and the policy form persists."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.career.database.models import TenantRow
from app.database import get_session_factory
from app.main import app
from app.pipeline.repository import PolicyRepository
from tests.conftest import AUTH_HEADERS

TENANT = f"dash-p2-{uuid.uuid4().hex[:8]}"
KEY = AUTH_HEADERS["X-API-Key"]


@pytest.fixture(scope="module", autouse=True)
def _tenant_override():
    session = get_session_factory()()
    session.add(TenantRow(id=TENANT, name="dash tenant"))
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
def client():
    c = TestClient(app, follow_redirects=False)
    c.get("/dashboard/", params={"key": KEY})
    return c


def test_pages_require_auth_and_render():
    anon = TestClient(app)
    for path in ("/dashboard/opportunities", "/dashboard/queue", "/dashboard/policy"):
        assert anon.get(path).status_code == 401


def test_opportunities_queue_and_policy_pages(client):
    page = client.get("/dashboard/opportunities")
    assert page.status_code == 200 and "Opportunities" in page.text and "No candidate opportunities yet" in page.text
    queue = client.get("/dashboard/queue")
    assert queue.status_code == 200 and "The queue is empty" in queue.text
    policy = client.get("/dashboard/policy")
    assert policy.status_code == 200 and "Enabled fit bands" in policy.text


def test_policy_form_persists(client):
    resp = client.post(
        "/dashboard/policy",
        data={
            "enabled_bands": ["HIGH", "MEDIUM"],
            "high_threshold": "75",
            "medium_threshold": "50",
            "daily_cap": "40",
            "weekly_cap": "200",
            "cooldown_days": "60",
            "minimum_eligibility": "LIKELY",
            "blocked_companies": "Evil Corp, Spam Inc",
            "lane_high": "AUTO",
            "lane_medium": "REVIEW",
            "lane_low": "MANUAL",
            "tailoring_high": "L2",
            "tailoring_medium": "L1",
            "tailoring_low": "L0",
        },
    )
    assert resp.status_code == 303 and "policy-saved" in resp.headers["location"]
    session = get_session_factory()()
    try:
        policy = PolicyRepository(session, TENANT).get()
    finally:
        session.close()
    assert [b.value for b in policy.enabled_bands] == ["HIGH", "MEDIUM"]
    assert policy.band_thresholds == {"HIGH": 75, "MEDIUM": 50}
    assert policy.blocked_companies == ["Evil Corp", "Spam Inc"]
    assert policy.lane_by_band["HIGH"].value == "AUTO" and policy.minimum_eligibility.value == "LIKELY"

    bad = client.post("/dashboard/policy", data={"high_threshold": "10", "medium_threshold": "50"})
    assert "error:validation_failed" in bad.headers["location"]
