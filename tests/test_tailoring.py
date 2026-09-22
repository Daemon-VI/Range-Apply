"""Tests for the Phase 4 tailoring engine (/api/v4/tailoring).

Seeds one JobRow + one JobMatchRow with real RequirementAssessmentRow rows
whose evidence_references point at real skill ids from
``data/career_seed.json``. Uses the real CareerBrainService (not mocked) so
the tests prove real evidence actually flows into the generated content.
"""

import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.timeutils import utc_now
from app.database import get_db
from app.intelligence.database.models import JobMatchRow, MatchRunRow, RequirementAssessmentRow
from app.jobs.database.models import Base, JobRow
from app.tailoring.api.routes import router as tailoring_router
from app.tailoring.generator import TailoringEngine
from tests.conftest import AUTH_HEADERS

# The tailoring router is not yet wired into app.main (another agent does
# that after the migration lands), so tests exercise it on a minimal FastAPI
# app of their own rather than importing app.main.app.
app = FastAPI()
app.include_router(tailoring_router)

_DB_PATH = "./test_tailoring_runner.db"
_engine = create_engine(f"sqlite:///{_DB_PATH}", connect_args={"check_same_thread": False})
_SessionLocal = sessionmaker(bind=_engine)

JOB_ID = str(uuid.uuid4())
MATCH_ID = str(uuid.uuid4())
RUN_ID = str(uuid.uuid4())
UNMATCHED_JOB_ID = str(uuid.uuid4())


def _override_get_db():
    db = _SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="module", autouse=True)
def _tailoring_test_db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)

    db = _SessionLocal()
    db.add(
        JobRow(
            id=JOB_ID,
            canonical_key="acme|backend engineer|remote",
            source="GREENHOUSE",
            source_job_id="tailoring-job-1",
            company="Acme Corp",
            title="Backend Engineer",
            original_title="Backend Engineer",
            description="Python/FastAPI backend role.",
            original_description="Python/FastAPI backend role.",
            location="Remote",
            source_url="https://example.com/careers/tailoring-job-1",
            content_hash="tailoring-job-1-hash",
            processing_status="NORMALIZED",
            job_status="ACTIVE",
        )
    )
    db.add(
        JobRow(
            id=UNMATCHED_JOB_ID,
            canonical_key="acme|unmatched role|remote",
            source="GREENHOUSE",
            source_job_id="tailoring-job-2",
            company="Acme Corp",
            title="Unmatched Role",
            original_title="Unmatched Role",
            description="Role with no match run.",
            original_description="Role with no match run.",
            location="Remote",
            source_url="https://example.com/careers/tailoring-job-2",
            content_hash="tailoring-job-2-hash",
            processing_status="NORMALIZED",
            job_status="ACTIVE",
        )
    )
    db.add(
        MatchRunRow(
            id=RUN_ID,
            policy_version="v1",
            engine_version="1.0.0",
            career_brain_version="v1",
            status="COMPLETED",
            started_at=utc_now(),
            completed_at=utc_now(),
        )
    )
    db.add(
        JobMatchRow(
            id=MATCH_ID,
            job_id=JOB_ID,
            run_id=RUN_ID,
            job_canonical_key="acme|backend engineer|remote",
            job_content_hash="tailoring-job-1-hash",
            eligibility_status="ELIGIBLE",
            fit_score=88,
            priority="HIGH",
            match_type="STRONG",
            confidence="HIGH",
            strengths=["Python", "FastAPI"],
            gaps=[],
            explanation="Strong overlap on backend skills.",
            evaluated_at=utc_now(),
        )
    )
    db.add_all(
        [
            RequirementAssessmentRow(
                job_match_id=MATCH_ID,
                requirement_name="Python",
                requirement_category="TECHNICAL",
                requirement_strictness="REQUIRED",
                status="MATCHED",
                evidence_strength="STRONG",
                evidence_references=["skill-python"],
                confidence="HIGH",
                weight=1.0,
                contribution=30.0,
            ),
            RequirementAssessmentRow(
                job_match_id=MATCH_ID,
                requirement_name="FastAPI",
                requirement_category="TECHNICAL",
                requirement_strictness="REQUIRED",
                status="MATCHED",
                evidence_strength="STRONG",
                evidence_references=["skill-fastapi"],
                confidence="HIGH",
                weight=1.0,
                contribution=25.0,
            ),
            RequirementAssessmentRow(
                job_match_id=MATCH_ID,
                requirement_name="Distributed systems",
                requirement_category="TECHNICAL",
                requirement_strictness="PREFERRED",
                status="MATCHED",
                evidence_strength="MODERATE",
                evidence_references=["ticket-engine"],
                confidence="MEDIUM",
                weight=0.5,
                contribution=10.0,
            ),
            RequirementAssessmentRow(
                job_match_id=MATCH_ID,
                requirement_name="Content writing",
                requirement_category="OTHER",
                requirement_strictness="PREFERRED",
                status="GAP",
                evidence_strength="NONE",
                evidence_references=[],
                confidence="LOW",
                weight=0.0,
                contribution=0.0,
            ),
        ]
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


def test_generate_produces_three_artifacts_with_real_evidence(client):
    resp = client.post("/api/v4/tailoring/generate", headers=AUTH_HEADERS, json={"job_id": JOB_ID})
    assert resp.status_code == 200, resp.text
    artifacts = resp.json()
    assert len(artifacts) == 3

    types = {a["artifact_type"] for a in artifacts}
    assert types == {"RESUME", "COVER_LETTER", "ANSWER"}

    resume = next(a for a in artifacts if a["artifact_type"] == "RESUME")
    # A real skill name from the seed data must appear - proof evidence flowed through.
    assert "Python" in resume["content"]
    assert resume["evidence_refs"]

    cover_letter = next(a for a in artifacts if a["artifact_type"] == "COVER_LETTER")
    assert "Acme Corp" in cover_letter["content"]
    assert "Backend Engineer" in cover_letter["content"]

    for a in artifacts:
        assert a["version"] == 1
        assert a["approved"] is False


def test_generate_without_match_raises_clear_error():
    db = _SessionLocal()
    try:
        with pytest.raises(ValueError, match="No match run exists"):
            TailoringEngine().generate(db, UNMATCHED_JOB_ID)
    finally:
        db.close()


def test_generate_via_api_without_match_returns_422(client):
    resp = client.post(
        "/api/v4/tailoring/generate", headers=AUTH_HEADERS, json={"job_id": UNMATCHED_JOB_ID}
    )
    assert resp.status_code == 422


def test_generate_unknown_job_returns_404(client):
    resp = client.post(
        "/api/v4/tailoring/generate", headers=AUTH_HEADERS, json={"job_id": "does-not-exist"}
    )
    assert resp.status_code == 404


def test_regenerate_bumps_version(client):
    resp = client.post("/api/v4/tailoring/generate", headers=AUTH_HEADERS, json={"job_id": JOB_ID})
    assert resp.status_code == 200
    artifacts = resp.json()
    resume = next(a for a in artifacts if a["artifact_type"] == "RESUME")
    assert resume["version"] == 2


def test_get_job_artifacts_ordered_newest_first(client):
    resp = client.get(f"/api/v4/tailoring/job/{JOB_ID}")
    assert resp.status_code == 200
    artifacts = resp.json()
    assert len(artifacts) == 6  # two generate() calls x 3 artifact types

    resume_versions = [a["version"] for a in artifacts if a["artifact_type"] == "RESUME"]
    assert resume_versions == sorted(resume_versions, reverse=True)


def test_approve_flips_flag(client):
    listing = client.get(f"/api/v4/tailoring/job/{JOB_ID}").json()
    artifact_id = listing[0]["id"]
    assert listing[0]["approved"] is False

    resp = client.post(f"/api/v4/tailoring/{artifact_id}/approve", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["approved"] is True


def test_approve_unknown_artifact_404(client):
    resp = client.post("/api/v4/tailoring/does-not-exist/approve", headers=AUTH_HEADERS)
    assert resp.status_code == 404
