"""Extension capture uses the one ingestion path; JSON-LD first, DOM fallback."""

import pytest

from app.jobs.database.models import JobRow
from app.jobs.models.enums import JobSourceType
from app.jobs.pipeline.discovery_service import JobDiscoveryService
from app.jobs.sources.capture import CapturedJob, captured_to_raw_job
from app.pipeline.database.models import OpportunityJobRow, OpportunityRow
from tests.discovery.conftest import fake_source, make_raw

JSON_LD = {
    "@context": "https://schema.org",
    "@graph": [
        {"@type": "WebPage", "name": "irrelevant"},
        {
            "@type": "JobPosting",
            "title": "Backend Engineer",
            "hiringOrganization": {"@type": "Organization", "name": "Acme"},
            "jobLocation": {"@type": "Place", "address": {"addressLocality": "Hyderabad", "addressCountry": "IN"}},
            "jobLocationType": "TELECOMMUTE",
            "datePosted": "2026-09-10T08:00:00Z",
            "validThrough": "2026-10-01T00:00:00Z",
            "identifier": {"@type": "PropertyValue", "value": "REQ-77"},
            "description": "<p>Python and PostgreSQL. Remote within India.</p>",
            "employmentType": "FULL_TIME",
        },
    ],
}


def test_json_ld_takes_precedence_over_dom_fields():
    capture = CapturedJob(url="https://jobs.example.com/acme/77?utm=x", title="Wrong Title", company="Wrong Co", json_ld=JSON_LD)
    raw, reason = captured_to_raw_job(capture)
    assert reason is None
    assert raw.source == JobSourceType.EXTENSION
    assert raw.raw_title == "Backend Engineer" and raw.raw_metadata["company_slug"] == "acme"
    assert raw.source_job_id == "REQ-77"
    assert raw.raw_location == "Hyderabad, IN"
    assert raw.raw_metadata["workplaceType"] == "REMOTE"
    assert raw.source_posted_at.year == 2026 and raw.source_deadline.month == 10
    assert raw.extraction_method == "extension_capture"


def test_dom_fallback_and_rejections():
    raw, reason = captured_to_raw_job(CapturedJob(url="https://x.example/job/1", title="QA Engineer", company="Beta", location="Pune", description="Manual testing"))
    assert reason is None and raw.source_job_id.startswith("cap-") and raw.raw_location == "Pune"
    assert captured_to_raw_job(CapturedJob(url="https://x.example/job/2", company="Beta")) == (None, "missing title")
    assert captured_to_raw_job(CapturedJob(url="https://x.example/job/3", title="X")) == (None, "missing company")
    same_url_a, _ = captured_to_raw_job(CapturedJob(url="https://x.example/job/9?ref=a", title="T", company="C"))
    same_url_b, _ = captured_to_raw_job(CapturedJob(url="https://x.example/job/9?ref=b", title="T", company="C"))
    assert same_url_a.source_job_id == same_url_b.source_job_id, "tracking params do not change identity"


@pytest.mark.asyncio
async def test_capture_runs_the_same_pipeline_and_converges_with_a_board_posting(db_session):
    service = JobDiscoveryService()
    service.register_source(JobSourceType.OTHER, fake_source(jobs=[make_raw("gh-1", title="Backend Engineer", company="acme", location="Remote")]))
    board_run = await service.run_discovery(db_session, JobSourceType.OTHER, "acme", company_name="Acme", trigger="test")
    assert board_run.jobs_new == 1 and board_run.opportunities_new == 1

    capture = CapturedJob(url="https://jobs.example.com/acme/77", json_ld=JSON_LD)
    run = await service.run_discovery(
        db_session, JobSourceType.EXTENSION, "acme", company_name="Acme", trigger="capture",
        close_missing=False, adapter_kwargs={"capture": capture},
    )
    assert run.status == "completed" and run.jobs_new == 1
    assert run.opportunities_new == 0 and run.opportunities_linked == 1, "same company/title/remote → same opportunity"
    assert db_session.query(OpportunityRow).count() == 1
    assert db_session.query(OpportunityJobRow).count() == 2
    captured_job = db_session.query(JobRow).filter_by(source=JobSourceType.EXTENSION.value).one()
    assert captured_job.remote_type == "REMOTE" and captured_job.freshness in ("FRESH", "RECENT", "AGING", "STALE")

    again = await service.run_discovery(
        db_session, JobSourceType.EXTENSION, "acme", trigger="capture", close_missing=False, adapter_kwargs={"capture": capture}
    )
    assert again.jobs_unchanged == 1 and again.jobs_new == 0
    assert db_session.query(JobRow).count() == 2, "the board posting was not closed by a capture run"
