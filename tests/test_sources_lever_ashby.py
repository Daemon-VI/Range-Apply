"""Tests for Lever and Ashby source adapters (Milestone 4)."""

import httpx
import pytest

from app.jobs.models.enums import JobSourceType
from app.jobs.sources.ashby import AshbySource
from app.jobs.sources.base import SourceError
from app.jobs.sources.lever import LeverSource

MOCK_LEVER_RESPONSE = [
    {
        "id": "lever-101",
        "text": "Full Stack Engineer - Core Product",
        "categories": {
            "commitment": "Full-time",
            "department": "Engineering",
            "location": "San Francisco, CA",
            "team": "Core",
        },
        "workplaceType": "hybrid",
        "description": "Plain text description of the full stack role",
        "descriptionHtml": "<div><p>HTML description of the full stack role</p></div>",
        "urls": {
            "apply": "https://jobs.lever.co/leverdemo/lever-101/apply",
            "list": "https://jobs.lever.co/leverdemo",
            "show": "https://jobs.lever.co/leverdemo/lever-101",
        },
        "salaryRange": {"currency": "USD", "interval": "per-year-salary", "min": 140000, "max": 180000},
        "createdAt": 1723500000000,
    }
]

MOCK_ASHBY_RESPONSE = {
    "jobs": [
        {
            "id": "ashby-202",
            "title": "Machine Learning Engineer",
            "department": "AI & Research",
            "team": "Inference",
            "location": "New York, NY",
            "secondaryLocations": ["Remote - US"],
            "workplaceType": "Remote",
            "employmentType": "FullTime",
            "descriptionHtml": "<p>Build production ML inference pipelines with PyTorch and FastAPI.</p>",
            "descriptionPlain": "Build production ML inference pipelines with PyTorch and FastAPI.",
            "jobUrl": "https://jobs.ashbyhq.com/ramp/ashby-202",
            "applyUrl": "https://jobs.ashbyhq.com/ramp/ashby-202/application",
            "compensationTierSummary": "$160k - $210k • 0.1% - 0.25%",
            "publishedAt": "2026-08-16T14:30:00Z",
        }
    ]
}


@pytest.mark.asyncio
async def test_lever_discover_success():
    def mock_transport(request: httpx.Request):
        assert "api.lever.co/v0/postings/leverdemo" in str(request.url)
        return httpx.Response(200, json=MOCK_LEVER_RESPONSE)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    adapter = LeverSource(client=client)

    raw_jobs = await adapter.discover("leverdemo")

    assert len(raw_jobs) == 1
    job = raw_jobs[0]
    assert job.source == JobSourceType.LEVER
    assert job.source_job_id == "lever-101"
    assert job.raw_title == "Full Stack Engineer - Core Product"
    assert job.raw_location == "San Francisco, CA"
    assert job.content_type == "html"
    assert job.raw_metadata["workplaceType"] == "hybrid"
    assert job.raw_metadata["commitment"] == "Full-time"


@pytest.mark.asyncio
async def test_lever_discover_404():
    def mock_transport(request: httpx.Request):
        return httpx.Response(404, json={"error": "Company not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    adapter = LeverSource(client=client)

    with pytest.raises(SourceError) as exc_info:
        await adapter.discover("invalid_company")

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_ashby_discover_success():
    def mock_transport(request: httpx.Request):
        assert "api.ashbyhq.com/posting-api/job-board/ramp" in str(request.url)
        return httpx.Response(200, json=MOCK_ASHBY_RESPONSE)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    adapter = AshbySource(client=client)

    raw_jobs = await adapter.discover("ramp")

    assert len(raw_jobs) == 1
    job = raw_jobs[0]
    assert job.source == JobSourceType.ASHBY
    assert job.source_job_id == "ashby-202"
    assert job.raw_title == "Machine Learning Engineer"
    assert job.raw_location == "New York, NY"
    assert job.raw_metadata["employmentType"] == "FullTime"
    assert job.raw_metadata["workplaceType"] == "Remote"
    assert "PyTorch" in job.raw_content


@pytest.mark.asyncio
async def test_ashby_discover_404():
    def mock_transport(request: httpx.Request):
        return httpx.Response(404, json={"error": "Not Found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    adapter = AshbySource(client=client)

    with pytest.raises(SourceError) as exc_info:
        await adapter.discover("unknown_board")

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_lever_live_payload_shape_targets_the_apply_page():
    """The live v0 API (Phase 13 real-world validation) puts hostedUrl/applyUrl
    at the top level and has no `urls` object; the application form is only
    on the `/apply` page, so that is what the job must point to."""
    live_shape = [{"id": "b74e88a1", "text": "Product Compliance Manager", "hostedUrl": "https://jobs.lever.co/nium/b74e88a1", "applyUrl": "https://jobs.lever.co/nium/b74e88a1/apply", "categories": {"location": "Singapore"}, "descriptionPlain": "x", "createdAt": 1723500000000}]

    def mock_transport(request: httpx.Request):
        return httpx.Response(200, json=live_shape)

    adapter = LeverSource(client=httpx.AsyncClient(transport=httpx.MockTransport(mock_transport)))
    job = (await adapter.discover("nium"))[0]
    assert job.source_url == "https://jobs.lever.co/nium/b74e88a1"
    assert job.raw_metadata["apply_url"] == "https://jobs.lever.co/nium/b74e88a1/apply"
    # without either shape the apply page is still derived from the posting
    live_shape[0].pop("applyUrl")
    job = (await adapter.discover("nium"))[0]
    assert job.raw_metadata["apply_url"] == "https://jobs.lever.co/nium/b74e88a1/apply"
