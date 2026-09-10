"""End-to-end test of the PRD-critical path: discovery -> normalization ->
deduplication -> persistence -> matching -> explanation.

No network is used. A fake :class:`JobSource` is registered on
:class:`JobDiscoveryService` and returns three hand-crafted ``RawJob``
instances:

* a strong match — Python/Go/PostgreSQL, squarely inside the candidate's
  verified skill set (see ``data/career_seed.json``);
* an irrelevant marketing role with no technical overlap;
* a job the candidate is technically strong for but is hard-ineligible for
  (it requires graduating by 2025; the candidate graduates 2027).

This exercises the full pipeline exactly as production would run it, against
the real Career Brain seed data, with nothing mocked below the source adapter.
"""

from typing import List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.intelligence.database.models  # noqa: F401 - registers Phase 3 tables on Base.metadata
from app.core.timeutils import utc_now
from app.intelligence.database.models import JobMatchRow
from app.intelligence.services.factory import build_orchestrator
from app.intelligence.services.match_persistence import run_matching, stale_job_ids
from app.jobs.database.models import Base, JobRow
from app.jobs.models.enums import JobSourceType
from app.jobs.models.raw_job import RawJob
from app.jobs.pipeline.discovery_service import JobDiscoveryService
from app.jobs.sources.base import JobSource

STRONG_JOB_CONTENT = """Backend Engineer at NimbusStack

We are building a real-time backend platform for developer tooling.

Requirements:
- Strong experience with Python and Go
- Hands-on experience with PostgreSQL
- Experience with Redis for queues

Nice to have:
- Experience with Docker
"""

IRRELEVANT_JOB_CONTENT = """Senior Content Marketing Manager at BrightLeaf Media

We are hiring a Content Marketing Manager to lead our brand storytelling and editorial calendar.

Requirements:
- 5+ years of experience in content marketing or brand strategy
- Experience with Kubernetes and AWS for managing our internal content tooling
- Excellent copywriting, editing, and storytelling skills
- Strong stakeholder management skills

Nice to have:
- Experience with major email newsletter platforms
"""

INELIGIBLE_JOB_CONTENT = """New Grad Software Engineer at TalentForge

We are hiring a New Grad Software Engineer with strong Python and Go skills to join our platform team.

Requirements:
- Strong experience with Python and Go
- Hands-on experience with PostgreSQL
- Bachelor's degree in Computer Science

Eligibility:
- Candidates must be graduating by 2025 to apply to this new grad program.
"""


class _FakeSource(JobSource):
    """Test double: hands back crafted RawJobs, no network access at all."""

    JOBS: List[RawJob] = []

    @property
    def source_type(self) -> JobSourceType:
        return JobSourceType.OTHER

    async def discover(self, identifier: str, **kwargs) -> List[RawJob]:
        return list(self.JOBS)


def _raw_job(job_id: str, title: str, content: str, company_slug: str) -> RawJob:
    return RawJob(
        source=JobSourceType.OTHER,
        source_job_id=job_id,
        source_url=f"https://example.com/careers/{job_id}",
        discovered_url=f"https://example.com/careers/{job_id}",
        raw_title=title,
        raw_content=content,
        content_type="markdown",
        raw_location="Remote",
        source_posted_at=utc_now(),
        raw_metadata={"company_slug": company_slug},
    )


@pytest.fixture
def db_session():
    """Isolated in-memory database carrying both Phase 2 and Phase 3 tables."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.mark.asyncio
async def test_discovery_to_matching_end_to_end(db_session):
    _FakeSource.JOBS = [
        _raw_job("e2e-strong", "Backend Engineer", STRONG_JOB_CONTENT, "NimbusStack"),
        _raw_job(
            "e2e-irrelevant",
            "Senior Content Marketing Manager",
            IRRELEVANT_JOB_CONTENT,
            "BrightLeaf Media",
        ),
        _raw_job(
            "e2e-ineligible",
            "New Grad Software Engineer",
            INELIGIBLE_JOB_CONTENT,
            "TalentForge",
        ),
    ]

    service = JobDiscoveryService()
    service.register_source(JobSourceType.OTHER, _FakeSource)

    # --- discovery --------------------------------------------------------
    run = await service.run_discovery(
        db=db_session,
        source_type=JobSourceType.OTHER,
        identifier="e2e-board",
        trigger="test",
    )

    assert run.status == "completed"
    assert run.jobs_new == 3
    assert run.jobs_failed == 0
    assert db_session.query(JobRow).count() == 3

    strong_job = db_session.query(JobRow).filter_by(source_job_id="e2e-strong").first()
    irrelevant_job = db_session.query(JobRow).filter_by(source_job_id="e2e-irrelevant").first()
    ineligible_job = db_session.query(JobRow).filter_by(source_job_id="e2e-ineligible").first()
    assert strong_job is not None and irrelevant_job is not None and ineligible_job is not None

    assert strong_job.company == "NimbusStack"
    assert irrelevant_job.company == "BrightLeaf Media"
    assert ineligible_job.company == "TalentForge"

    for job in (strong_job, irrelevant_job, ineligible_job):
        assert job.posted_at is not None, f"posted_at not populated for {job.source_job_id}"

    # --- matching -----------------------------------------------------
    orchestrator = build_orchestrator()
    match_run = run_matching(db=db_session, orchestrator=orchestrator, trigger="test")

    assert match_run.status == "COMPLETED"
    assert match_run.jobs_matched == 3
    assert match_run.jobs_failed == 0

    def _match_for(job: JobRow) -> JobMatchRow:
        row = (
            db_session.query(JobMatchRow)
            .filter_by(job_id=job.id, run_id=match_run.id)
            .first()
        )
        assert row is not None, f"no persisted match for {job.source_job_id}"
        return row

    strong_match = _match_for(strong_job)
    irrelevant_match = _match_for(irrelevant_job)
    ineligible_match = _match_for(ineligible_job)

    # The Python/Go/PostgreSQL role, backed by verified skills and real
    # project evidence, must clearly outrank the unrelated marketing role.
    assert strong_match.fit_score > irrelevant_match.fit_score

    # A hard eligibility gate (graduation year) always caps score and priority,
    # independent of how strong the technical match would otherwise be.
    assert ineligible_match.eligibility_status == "INELIGIBLE"
    assert ineligible_match.priority == "IGNORE"
    assert any("2025" in reason for reason in ineligible_match.blocking_reasons)

    # Every match must explain itself with something concrete - a matched
    # strength, a missing requirement, or a hard blocker - never a blank or
    # purely generic explanation.
    for match in (strong_match, irrelevant_match, ineligible_match):
        assert match.explanation.strip()
        named = match.strengths or match.gaps or match.blocking_reasons
        assert named, f"match {match.id} names no concrete strength/gap/blocker"
        assert any(item.split(" (")[0] in match.explanation for item in named)

    assert strong_match.strengths, "strong job should have named strengths"
    assert "Python" in strong_match.strengths or "Go" in strong_match.strengths

    # --- staleness --------------------------------------------------------
    assert list(stale_job_ids(db_session)) == []

    strong_job.content_hash = "mutated-hash-for-staleness-check"
    db_session.commit()

    stale_ids = list(stale_job_ids(db_session))
    assert stale_ids == [strong_job.id]
