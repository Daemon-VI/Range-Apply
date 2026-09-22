"""Audit regressions (2026-09-14): what the public board adapters hand to extraction.

Shapes are taken from the live public APIs (read-only probes of lever ``zeta``,
greenhouse ``highradius``, ashby ``notion``), reduced to the fields that matter.
"""

import httpx
import pytest

from app.intelligence.extraction.seniority import experience_statements
from app.jobs.normalization.normalizer import JobNormalizer, validate_raw_job
from app.jobs.sources.ashby import AshbySource
from app.jobs.sources.greenhouse import GreenhouseSource
from app.jobs.sources.lever import LeverSource

LEVER_LIVE_SHAPE = [
    {
        "id": "zeta-1",
        "text": "Backend Engineer",
        "hostedUrl": "https://jobs.lever.co/zeta/zeta-1",
        "applyUrl": "https://jobs.lever.co/zeta/zeta-1/apply",
        "categories": {"commitment": "Full-time", "location": "Hyderabad", "allLocations": ["Hyderabad"]},
        "workplaceType": "onsite",
        # v0 `description` is only the opening, and it is HTML.
        "description": "<div><b>About Us</b></div><div>Build the future of banking.</div>",
        "descriptionPlain": "About Us\nBuild the future of banking.",
        "lists": [
            {"text": "Responsibilities", "content": "<li>Design and build payment services</li>"},
            {"text": "Experience and Qualification", "content": "<li>5+ years of experience with Java and Kafka</li><li>Bachelor's degree in Computer Science</li>"},
        ],
        "additional": "<div>Zeta is an equal opportunity employer.</div>",
        "createdAt": 1723500000000,
    }
]


def _client(payload):
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))


@pytest.mark.asyncio
async def test_lever_content_includes_lists_and_additional_sections():
    raw = (await LeverSource(client=_client(LEVER_LIVE_SHAPE)).discover("zeta"))[0]
    assert raw.content_type == "html"
    assert "5+ years of experience with Java" in raw.raw_content
    assert "Responsibilities" in raw.raw_content and "equal opportunity employer" in raw.raw_content
    job = JobNormalizer().normalize(raw)
    assert any("5+ years of experience" in q for q in job.qualifications)
    assert "Kafka" in job.description
    assert [s.years for s in experience_statements([job.title, *job.qualifications, job.description])][:1] == [5]


@pytest.mark.asyncio
async def test_lever_plain_only_payload_is_unchanged():
    payload = [{"id": "p1", "text": "Analyst", "hostedUrl": "https://jobs.lever.co/x/p1", "categories": {"location": "Pune"}, "descriptionPlain": "x"}]
    raw = (await LeverSource(client=_client(payload)).discover("x"))[0]
    assert raw.raw_content == "x" and raw.content_type == "plain"


@pytest.mark.asyncio
async def test_greenhouse_escaped_content_is_unescaped_before_extraction():
    escaped = "&lt;h3&gt;Requirements&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;3+ years of experience with Python&lt;/li&gt;&lt;/ul&gt;"
    payload = {"jobs": [{"id": 11, "title": "Backend Engineer", "absolute_url": "https://boards.greenhouse.io/highradius/jobs/11", "content": escaped, "location": {"name": "Hyderabad, Telangana, India"}}]}
    raw = (await GreenhouseSource(client=_client(payload)).discover("highradius"))[0]
    assert raw.raw_content.startswith("<h3>")
    job = JobNormalizer().normalize(raw)
    assert "<" not in job.description and "3+ years of experience with Python" in job.description
    assert job.qualifications == ["3+ years of experience with Python"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "payload"),
    [
        (GreenhouseSource, {"jobs": [{"id": 1, "title": None, "content": None}, {"id": 2, "title": "Engineer", "content": "<p>ok</p>"}]}),
        (LeverSource, [{"id": "a", "text": None}, {"id": "b", "text": "Engineer", "hostedUrl": "https://jobs.lever.co/x/b"}]),
        (AshbySource, {"jobs": [{"id": "a", "title": None}, {"id": "b", "title": "Engineer"}]}),
    ],
)
async def test_a_null_title_is_a_rejected_record_not_a_failed_board(source, payload):
    raws = await source(client=_client(payload)).discover("board")
    assert len(raws) == 2
    assert validate_raw_job(raws[0]) == "missing title"
    assert validate_raw_job(raws[1]) is None
