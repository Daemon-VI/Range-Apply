"""Tests for Greenhouse source adapter (Milestone 3)."""

import httpx
import pytest

from app.jobs.models.enums import JobSourceType
from app.jobs.sources.base import SourceError
from app.jobs.sources.greenhouse import GreenhouseSource

MOCK_GREENHOUSE_RESPONSE = {
    "jobs": [
        {
            "id": 4001,
            "title": "Software Engineer - Infrastructure",
            "absolute_url": "https://boards.greenhouse.io/stripe/jobs/4001",
            "location": {"name": "Seattle, WA, USA"},
            "departments": [{"id": 10, "name": "Engineering"}],
            "offices": [{"id": 20, "name": "Seattle Office"}],
            "content": "<p>We are looking for an Infrastructure Engineer with Go and Kubernetes experience.</p>",
            "updated_at": "2026-08-15T12:00:00Z",
            "requisition_id": "REQ-100",
            "metadata": [],
        },
        {
            "id": 4002,
            "title": "Backend Engineering Intern",
            "absolute_url": "https://boards.greenhouse.io/stripe/jobs/4002",
            "location": {"name": "San Francisco, CA"},
            "departments": [{"id": 10, "name": "Engineering"}],
            "offices": [{"id": 21, "name": "San Francisco Office"}],
            "content": "<p>Summer 2027 internship for Computer Science students.</p>",
            "updated_at": "2026-08-18T10:00:00Z",
            "requisition_id": "REQ-101",
            "metadata": [],
        },
    ]
}


@pytest.mark.asyncio
async def test_greenhouse_discover_success():
    def mock_transport(request: httpx.Request):
        assert "boards-api.greenhouse.io/v1/boards/stripe/jobs" in str(request.url)
        return httpx.Response(200, json=MOCK_GREENHOUSE_RESPONSE)

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    adapter = GreenhouseSource(client=client)

    raw_jobs = await adapter.discover("stripe")

    assert len(raw_jobs) == 2
    job1 = raw_jobs[0]
    assert job1.source == JobSourceType.GREENHOUSE
    assert job1.source_job_id == "4001"
    assert job1.raw_title == "Software Engineer - Infrastructure"
    assert job1.raw_location == "Seattle, WA, USA"
    assert "Kubernetes" in job1.raw_content
    assert job1.content_type == "html"
    assert job1.raw_metadata["board_token"] == "stripe"

    job2 = raw_jobs[1]
    assert job2.source_job_id == "4002"
    assert job2.raw_title == "Backend Engineering Intern"


@pytest.mark.asyncio
async def test_greenhouse_discover_404_not_found():
    def mock_transport(request: httpx.Request):
        return httpx.Response(404, json={"error": "board not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    adapter = GreenhouseSource(client=client)

    with pytest.raises(SourceError) as exc_info:
        await adapter.discover("nonexistent_board_12345")

    assert exc_info.value.status_code == 404
    assert exc_info.value.identifier == "nonexistent_board_12345"


@pytest.mark.asyncio
async def test_greenhouse_discover_empty_board():
    def mock_transport(request: httpx.Request):
        return httpx.Response(200, json={"jobs": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_transport))
    adapter = GreenhouseSource(client=client)

    raw_jobs = await adapter.discover("empty_board")
    assert raw_jobs == []
