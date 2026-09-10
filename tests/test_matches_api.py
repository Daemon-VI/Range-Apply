"""Tests for the Phase 3 intelligence API (/api/v3/matches) - real persisted
matches, not stubs.

Two JobRows are seeded directly into the database: one squarely inside the
candidate's verified skill set (Python/Go/PostgreSQL - see
``data/career_seed.json``), one with zero technical overlap. A single
``recalculate`` run - through the real orchestrator and the real Career
Brain, nothing mocked - is then used to exercise listing, detail, filtering
and sorting.
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.intelligence.database.models  # noqa: F401 - registers Phase 3 tables
from app.core.timeutils import utc_now
from app.database import get_db
from app.jobs.database.models import Base, JobRow
from app.main import app
from tests.conftest import AUTH_HEADERS

_DB_PATH = "./test_matches_api_runner.db"
_engine = create_engine(f"sqlite:///{_DB_PATH}", connect_args={"check_same_thread": False})
_SessionLocal = sessionmaker(bind=_engine)

STRONG_JOB_ID = str(uuid.uuid4())
WEAK_JOB_ID = str(uuid.uuid4())

STRONG_DESCRIPTION = """We are building a real-time backend platform for developer tooling.

Requirements:
- Strong experience with Python and Go
- Hands-on experience with PostgreSQL
- Experience with Redis for queues
"""

WEAK_DESCRIPTION = """We are hiring a Content Writer to produce travel guides and destination articles.

Requirements:
- 3+ years of experience in travel writing or journalism
- Excellent storytelling and editing skills
- Familiarity with SEO best practices for blog content
"""


def _override_get_db():
    db = _SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="module", autouse=True)
def _matches_test_db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)

    db = _SessionLocal()
    db.add(
        JobRow(
            id=STRONG_JOB_ID,
            canonical_key="nimbusstack|backend engineer|remote",
            source="GREENHOUSE",
            source_job_id="matches-api-strong",
            company="NimbusStack",
            title="Backend Engineer",
            original_title="Backend Engineer",
            description=STRONG_DESCRIPTION,
            original_description=STRONG_DESCRIPTION,
            location="Remote",
            locations=["Remote"],
            remote_type="REMOTE",
            employment_type="FULL_TIME",
            experience_level="MID",
            required_skills=["Python", "Go", "PostgreSQL", "Redis"],
            source_url="https://example.com/careers/matches-api-strong",
            posted_at=utc_now(),
            content_hash="matches-api-strong-hash",
            processing_status="NORMALIZED",
            job_status="ACTIVE",
        )
    )
    db.add(
        JobRow(
            id=WEAK_JOB_ID,
            canonical_key="wanderlust travel|content writer|remote",
            source="GREENHOUSE",
            source_job_id="matches-api-weak",
            company="Wanderlust Travel",
            title="Content Writer",
            original_title="Content Writer",
            description=WEAK_DESCRIPTION,
            original_description=WEAK_DESCRIPTION,
            location="Remote",
            locations=["Remote"],
            remote_type="REMOTE",
            employment_type="FULL_TIME",
            experience_level="MID",
            required_skills=[],
            source_url="https://example.com/careers/matches-api-weak",
            posted_at=utc_now(),
            content_hash="matches-api-weak-hash",
            processing_status="NORMALIZED",
            job_status="ACTIVE",
        )
    )
    db.commit()
    db.close()

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _override_get_db
    yield
    if previous is not None:
        app.dependency_overrides[get_db] = previous
    else:
        app.dependency_overrides.pop(get_db, None)
    Base.metadata.drop_all(bind=_engine)
    _engine.dispose()
    if os.path.exists(_DB_PATH):
        try:
            os.remove(_DB_PATH)
        except OSError:
            pass


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_matches_empty_before_any_run(client):
    """No match run has ever executed: an empty list, not an error."""
    resp = client.get("/api/v3/matches/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["run_id"] is None
    assert data["items"] == []


@pytest.fixture(scope="module")
def completed_run(client):
    """Runs the real matching engine once; every dependent test reuses it."""
    resp = client.post(
        "/api/v3/matches/recalculate",
        params={"wait": "true"},
        headers=AUTH_HEADERS,
        json={},
    )
    # wait=true runs synchronously and hands back the finished run; FastAPI
    # returns 200 for it despite the route's 202 default (that default only
    # applies to the fire-and-forget background path - see test_api_security).
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "COMPLETED"
    assert body["jobs_matched"] == 2
    return body


def test_recalculate_persists_real_scored_matches(client, completed_run):
    resp = client.get("/api/v3/matches/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == completed_run["id"]
    assert data["total"] == 2

    by_job = {item["job_id"]: item for item in data["items"]}
    strong = by_job[STRONG_JOB_ID]
    weak = by_job[WEAK_JOB_ID]

    # Not stubs: these values are derived from the seeded jobs' own text.
    assert strong["title"] == "Backend Engineer"
    assert strong["company"] == "NimbusStack"
    assert weak["title"] == "Content Writer"
    assert weak["company"] == "Wanderlust Travel"

    for item in (strong, weak):
        assert item["fit_score"] > 0
        assert item["component_scores"]
        assert item["eligibility_status"]

    # The Python/Go/PostgreSQL role must clearly outscore the unrelated one.
    assert strong["fit_score"] > weak["fit_score"]


def test_match_detail_includes_explanation_and_assessments(client, completed_run):
    resp = client.get(f"/api/v3/matches/{STRONG_JOB_ID}")
    assert resp.status_code == 200
    data = resp.json()

    assert data["job_id"] == STRONG_JOB_ID
    assert data["title"] == "Backend Engineer"
    assert data["explanation"].strip()
    assert data["policy_version"]
    assert data["run_id"] == completed_run["id"]

    assert data["assessments"], "expected at least one requirement assessment"
    for assessment in data["assessments"]:
        assert "weight" in assessment
        assert "contribution" in assessment
    assert any(a["contribution"] > 0 for a in data["assessments"])


def test_match_detail_404_for_unknown_job(client, completed_run):
    resp = client.get("/api/v3/matches/does-not-exist")
    assert resp.status_code == 404


def test_filters_min_score_eligibility_priority_company(client, completed_run):
    listing = client.get("/api/v3/matches/").json()["items"]
    by_job = {item["job_id"]: item for item in listing}
    strong = by_job[STRONG_JOB_ID]
    weak = by_job[WEAK_JOB_ID]

    # min_score excludes the lower-scoring job once the threshold sits
    # strictly between the two real scores.
    threshold = min(weak["fit_score"] + 1, 100)
    assert threshold <= strong["fit_score"]
    resp = client.get("/api/v3/matches/", params={"min_score": threshold})
    ids = {item["job_id"] for item in resp.json()["items"]}
    assert ids == {STRONG_JOB_ID}

    resp = client.get("/api/v3/matches/", params={"eligibility": strong["eligibility_status"]})
    assert STRONG_JOB_ID in {item["job_id"] for item in resp.json()["items"]}

    resp = client.get("/api/v3/matches/", params={"priority": strong["priority"]})
    assert STRONG_JOB_ID in {item["job_id"] for item in resp.json()["items"]}

    resp = client.get("/api/v3/matches/", params={"company": "NimbusStack"})
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["job_id"] == STRONG_JOB_ID


def test_sort_by_score_orders_results(client, completed_run):
    desc = client.get(
        "/api/v3/matches/", params={"sort": "score", "order": "desc"}
    ).json()["items"]
    asc = client.get(
        "/api/v3/matches/", params={"sort": "score", "order": "asc"}
    ).json()["items"]

    assert [item["job_id"] for item in desc] == list(
        reversed([item["job_id"] for item in asc])
    )
    assert desc[0]["fit_score"] >= desc[-1]["fit_score"]
    assert asc[0]["fit_score"] <= asc[-1]["fit_score"]


def test_match_runs_lists_completed_run(client, completed_run):
    resp = client.get("/api/v3/matches/runs")
    assert resp.status_code == 200
    runs = resp.json()
    assert any(
        r["id"] == completed_run["id"] and r["status"] == "COMPLETED" and r["jobs_matched"] > 0
        for r in runs
    )
