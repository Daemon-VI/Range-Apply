"""Discovery API and dashboard additions over the shared test database."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.database import get_session_factory
from app.jobs.database.models import JobRow, SourceHealthRow
from app.main import app
from app.pipeline.database.models import OpportunityRow
from tests.conftest import AUTH_HEADERS

SUFFIX = uuid.uuid4().hex[:6]
COMPANY = f"Capco {SUFFIX}"


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    session = get_session_factory()()
    try:
        for job in session.query(JobRow).filter(JobRow.company == COMPANY).all():
            session.delete(job)
        for opp in session.query(OpportunityRow).filter(OpportunityRow.company == COMPANY).all():
            session.delete(opp)
        for row in session.query(SourceHealthRow).filter(SourceHealthRow.source_identifier.like(f"%{SUFFIX}%")).all():
            session.delete(row)
        session.commit()
    finally:
        session.close()


def test_capture_endpoint_ingests_and_reports(client):
    body = {
        "url": f"https://jobs.example.com/{SUFFIX}/1",
        "title": "Backend Engineer",
        "company": COMPANY,
        "location": "Remote",
        "description": "Python and SQL. Remote.",
    }
    assert client.post("/api/v2/discovery/capture", json=body).status_code == 401
    resp = client.post("/api/v2/discovery/capture", json=body, headers=AUTH_HEADERS)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["accepted"] and data["job_id"] and data["opportunity_id"]
    assert data["run"]["source"] == "EXTENSION" and data["run"]["jobs_new"] == 1 and data["run"]["trigger"] == "capture"

    again = client.post("/api/v2/discovery/capture", json=body, headers=AUTH_HEADERS).json()
    assert again["job_id"] == data["job_id"] and again["run"]["jobs_unchanged"] == 1

    bad = client.post("/api/v2/discovery/capture", json={"url": "https://x.example/none", "company": COMPANY}, headers=AUTH_HEADERS)
    assert bad.status_code == 422 and bad.json()["error"]["details"]["reason"] == "missing title"

    detail = client.get(f"/api/v2/jobs/{data['job_id']}").json()
    assert detail["source_references"][0]["source"] == "EXTENSION"
    listed = client.get("/api/v2/jobs", params={"company": COMPANY}).json()
    assert listed["items"][0]["freshness"] in ("FRESH", "RECENT", "AGING", "STALE", "UNKNOWN")


def test_sources_register_list_and_run_due(client):
    ident = f"board-{SUFFIX}"
    resp = client.post("/api/v2/discovery/sources", json={"source": "GREENHOUSE", "identifier": ident, "company_name": COMPANY, "poll_interval_minutes": 120}, headers=AUTH_HEADERS)
    assert resp.status_code == 200 and resp.json()["poll_interval_minutes"] == 120 and resp.json()["enabled"]
    rows = client.get("/api/v2/discovery/sources").json()
    mine = next(r for r in rows if r["source_identifier"] == ident)
    assert mine["runs_total"] == 0 and mine["success_rate"] is None
    disabled = client.post("/api/v2/discovery/sources", json={"source": "GREENHOUSE", "identifier": ident, "enabled": False}, headers=AUTH_HEADERS).json()
    assert disabled["enabled"] is False
    # Disabled boards are not scheduled; the endpoint still answers 202 with a count.
    due = client.post("/api/v2/discovery/run-due", headers=AUTH_HEADERS)
    assert due.status_code == 202 and "due source(s) scheduled" in due.json()["detail"]
    assert client.get("/api/v2/discovery/circuits").status_code == 200


def test_run_batch_registers_boards_and_resume_validates(client):
    ident = f"batch-{SUFFIX}"
    resp = client.post(
        "/api/v2/discovery/run-batch",
        json={"targets": [{"source": "LEVER", "identifier": ident, "company_name": COMPANY, "poll_interval_minutes": 90}]},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 202
    rows = {r["source_identifier"]: r for r in client.get("/api/v2/discovery/sources").json()}
    assert rows[ident]["poll_interval_minutes"] == 90
    assert client.post("/api/v2/discovery/runs/does-not-exist/resume", headers=AUTH_HEADERS).status_code == 404

    runs = client.get("/api/v2/discovery/runs", params={"source": "EXTENSION"}).json()
    assert runs and {"jobs_unchanged", "opportunities_new", "ai_calls", "network_requests", "checkpoint"} <= set(runs[0])


def test_dashboard_pages_render(client):
    key = AUTH_HEADERS["X-API-Key"]
    assert "Source health" in client.get("/dashboard/sources", params={"key": key}).text
    runs = client.get("/dashboard/runs", params={"key": key}).text
    assert "Opp new / linked" in runs and "AI calls / hits" in runs
