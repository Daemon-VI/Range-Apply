"""Tests for the Phase 3 shortlist / ranking dashboard (PRD P3.8).

Seeds two jobs with deliberately contrasting fit - one whose required skills
overlap heavily with the Career Brain seed data, one that both misses on
skills and fails the graduation-year hard gate - then runs the real matching
pipeline (``build_orchestrator`` + ``run_matching``) so the dashboard is
exercised against real persisted matches rather than hand-built fixtures.
"""

import html as html_lib
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.database import get_session_factory
from app.intelligence.database.models import JobMatchRow, RequirementAssessmentRow
from app.intelligence.services.factory import build_orchestrator
from app.intelligence.services.match_persistence import run_matching
from app.jobs.database.models import JobRow
from app.main import app
from tests.conftest import AUTH_HEADERS

# The dashboard is guarded by require_dashboard_auth, which reads the key
# from ?key=... (or a cookie set from a prior request), not X-API-Key.
DASH_KEY = AUTH_HEADERS["X-API-Key"]


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(scope="module")
def seeded_run():
    """Seed two contrasting jobs and run a real match over them.

    Module-scoped and lazily created: nothing here runs until a test first
    requests this fixture, which is what lets
    ``test_shortlist_empty_state_before_any_match_run`` observe a database
    with zero match runs.
    """
    db = get_session_factory()()
    try:
        strong = JobRow(
            id="shortlist-test-job-strong",
            canonical_key="shortlist-test|strong-backend-engineer|remote",
            source="GREENHOUSE",
            source_job_id="shortlist-strong-1",
            company="Acme Systems",
            title="Backend Engineer",
            original_title="Backend Engineer",
            description=(
                "Build backend services in Python and Go using FastAPI, "
                "Docker and SQL."
            ),
            original_description=(
                "Build backend services in Python and Go using FastAPI, "
                "Docker and SQL."
            ),
            location="Remote",
            locations=["Remote"],
            remote_type="REMOTE",
            employment_type="FULL_TIME",
            experience_level="ENTRY_LEVEL",
            required_skills=["Python", "Go", "FastAPI", "Docker", "SQL"],
            source_url="https://boards.greenhouse.io/acme/jobs/1",
            content_hash="shortlist-hash-strong",
            processing_status="NORMALIZED",
            job_status="ACTIVE",
            posted_at=datetime.utcnow() - timedelta(days=3),
        )
        weak = JobRow(
            id="shortlist-test-job-weak",
            canonical_key="shortlist-test|marketing-intern|onsite",
            source="LEVER",
            source_job_id="shortlist-weak-1",
            company="Zenith Marketing",
            title="Marketing Intern",
            original_title="Marketing Intern",
            description="Support campaigns using Adobe Photoshop and SEO strategy.",
            original_description="Support campaigns using Adobe Photoshop and SEO strategy.",
            location="New York, NY",
            locations=["New York, NY"],
            remote_type="ONSITE",
            employment_type="INTERNSHIP",
            experience_level="ENTRY_LEVEL",
            required_skills=["Adobe Photoshop", "SEO Strategy", "Content Writing"],
            source_url="https://jobs.lever.co/zenith/1",
            content_hash="shortlist-hash-weak",
            processing_status="NORMALIZED",
            job_status="ACTIVE",
            # Career Brain seed graduates 2027 - this guarantees INELIGIBLE via
            # the graduation hard gate, deterministically, instead of hoping
            # the scoring engine alone produces a lower number.
            graduation_year_requirement=2020,
            posted_at=None,
        )
        db.add_all([strong, weak])
        db.commit()

        run = run_matching(
            db=db,
            orchestrator=build_orchestrator(),
            job_ids=[strong.id, weak.id],
            trigger="test",
        )
        assert run.status in ("COMPLETED", "PARTIAL"), f"match run failed: {run.errors}"

        match_strong = db.query(JobMatchRow).filter_by(job_id=strong.id, run_id=run.id).first()
        match_weak = db.query(JobMatchRow).filter_by(job_id=weak.id, run_id=run.id).first()
        assert match_strong is not None, "strong job was not scored"
        assert match_weak is not None, "weak job was not scored"

        return {
            "run_id": run.id,
            "strong_id": strong.id,
            "weak_id": weak.id,
            "strong_title": strong.title,
            "weak_title": weak.title,
            "strong_score": match_strong.fit_score,
            "weak_score": match_weak.fit_score,
            "weak_eligibility": match_weak.eligibility_status,
        }
    finally:
        db.close()


def _get(client, path, **params):
    params.setdefault("key", DASH_KEY)
    return client.get(path, params=params)


def test_shortlist_empty_state_before_any_match_run(client):
    """No match run has ever executed: a clear empty state, not a 500."""
    response = _get(client, "/dashboard/shortlist")
    assert response.status_code == 200
    assert "/api/v3/matches/recalculate" in response.text


def test_shortlist_lists_seeded_job_with_its_fit_score(client, seeded_run):
    response = _get(client, "/dashboard/shortlist", limit=50)
    assert response.status_code == 200
    assert seeded_run["strong_title"] in response.text
    assert str(seeded_run["strong_score"]) in response.text


def test_shortlist_sort_by_score_desc_orders_higher_scoring_job_first(client, seeded_run):
    assert seeded_run["strong_score"] > seeded_run["weak_score"], (
        "fixture assumption broken: the two seeded jobs did not score differently"
    )
    response = _get(client, "/dashboard/shortlist", sort="score", order="desc", limit=50)
    assert response.status_code == 200
    html = response.text
    strong_index = html.index(seeded_run["strong_title"])
    weak_index = html.index(seeded_run["weak_title"])
    assert strong_index < weak_index, "higher-scoring job should render before the lower-scoring one"


def test_shortlist_filter_by_eligibility_removes_other_job(client, seeded_run):
    assert seeded_run["weak_eligibility"] == "INELIGIBLE"
    response = _get(client, "/dashboard/shortlist", eligibility="INELIGIBLE", limit=50)
    assert response.status_code == 200
    assert seeded_run["weak_title"] in response.text
    assert seeded_run["strong_title"] not in response.text


def test_shortlist_filter_by_min_score_removes_low_scoring_job(client, seeded_run):
    threshold = seeded_run["weak_score"] + 1
    assert threshold <= seeded_run["strong_score"], (
        "fixture assumption broken: not enough score spread to filter on"
    )
    response = _get(client, "/dashboard/shortlist", min_score=threshold, limit=50)
    assert response.status_code == 200
    assert seeded_run["strong_title"] in response.text
    assert seeded_run["weak_title"] not in response.text


def test_match_detail_renders_explanation_and_requirement_row(client, seeded_run):
    db = get_session_factory()()
    try:
        match = (
            db.query(JobMatchRow)
            .filter_by(job_id=seeded_run["strong_id"], run_id=seeded_run["run_id"])
            .first()
        )
        assessments = (
            db.query(RequirementAssessmentRow).filter_by(job_match_id=match.id).all()
        )
        assert assessments, "expected at least one requirement assessment for the strong job"
        explanation = match.explanation
    finally:
        db.close()

    response = _get(client, f"/dashboard/shortlist/{seeded_run['strong_id']}")
    assert response.status_code == 200
    # Jinja HTML-escapes text (quotes, apostrophes) but never touches "\n", so
    # unescaping is enough to check the explanation - and its line breaks -
    # survived rendering unchanged.
    rendered = html_lib.unescape(response.text)
    assert explanation in rendered, "explanation text (with its line breaks) should render verbatim"
    assert "\n" in explanation, "fixture assumption broken: explanation has no line breaks to preserve"
    assert assessments[0].requirement_name in rendered or (
        assessments[0].requirement_original_text and assessments[0].requirement_original_text in rendered
    )


def test_match_detail_unknown_job_id_returns_404(client, seeded_run):
    response = _get(client, "/dashboard/shortlist/does-not-exist-job-id")
    assert response.status_code == 404
