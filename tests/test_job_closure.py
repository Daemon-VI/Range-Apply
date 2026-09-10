"""Tests for the stale-job closure sweep: should_sweep gating and close_missing_jobs.

Also covers the end-to-end path through JobDiscoveryService with a fake,
in-process JobSource (no network), and reopening via the deduplicator when a
closed job reappears on its board.
"""

from datetime import timedelta
from typing import List, Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.timeutils import db_now
from app.jobs.database.models import Base, JobRow
from app.jobs.deduplication.deduplicator import JobDeduplicator
from app.jobs.models.enums import EmploymentType, JobSourceType, JobStatus, RemoteType
from app.jobs.models.job import NormalizedJob
from app.jobs.models.raw_job import RawJob
from app.jobs.pipeline.closure import close_missing_jobs, should_sweep
from app.jobs.pipeline.discovery_service import JobDiscoveryService
from app.jobs.sources.base import JobSource, SourceError

FAKE_SOURCE_TYPE = JobSourceType.OTHER


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def make_job_row(
    db,
    source_job_id: str,
    source: JobSourceType = JobSourceType.GREENHOUSE,
    source_identifier: str = "acme",
    company: str = "Acme",
    status: JobStatus = JobStatus.ACTIVE,
    closed_at=None,
) -> JobRow:
    """Inserts a JobRow directly, bypassing normalization/dedup, for closure tests."""
    job = JobRow(
        canonical_key=f"{company.lower()}|{source.value.lower()}|{source_identifier}|{source_job_id}",
        source=source.value,
        source_job_id=source_job_id,
        source_identifier=source_identifier,
        company=company,
        title="Backend Engineer",
        original_title="Backend Engineer",
        source_url=f"https://example.com/{source_identifier}/{source_job_id}",
        content_hash=f"hash-{source_identifier}-{source_job_id}",
        job_status=status.value,
        closed_at=closed_at,
    )
    db.add(job)
    db.flush()
    return job


def make_normalized_job(
    source_job_id: str = "101",
    source: JobSourceType = JobSourceType.GREENHOUSE,
    source_identifier: str = "acme",
    company: str = "Acme",
    title: str = "Backend Engineer",
    content_hash: str = "hash_v1",
) -> NormalizedJob:
    return NormalizedJob(
        canonical_key=f"{company.lower()}|{title.lower()}|{source_identifier}",
        source=source,
        source_job_id=source_job_id,
        source_identifier=source_identifier,
        company=company,
        title=title,
        original_title=title,
        description="A backend engineer role",
        remote_type=RemoteType.HYBRID,
        employment_type=EmploymentType.FULL_TIME,
        source_url=f"https://example.com/{source_identifier}/{source_job_id}",
        content_hash=content_hash,
        job_status=JobStatus.ACTIVE,
    )


def make_raw_job(source_job_id: str, identifier: str = "acme") -> RawJob:
    return RawJob(
        source=FAKE_SOURCE_TYPE,
        source_job_id=source_job_id,
        source_url=f"https://example.com/{identifier}/jobs/{source_job_id}",
        discovered_url=f"https://example.com/{identifier}/jobs/{source_job_id}",
        raw_title=f"Backend Engineer {source_job_id}",
        raw_content="<p>Looking for someone with Python and SQL experience.</p>",
        raw_location="Remote",
        raw_metadata={"board_token": identifier},
    )


def make_fake_source(jobs: Optional[List[RawJob]] = None, error: Optional[Exception] = None) -> type:
    class _FakeSource(JobSource):
        @property
        def source_type(self) -> JobSourceType:
            return FAKE_SOURCE_TYPE

        async def discover(self, identifier: str, **kwargs) -> List[RawJob]:
            if error is not None:
                raise error
            return list(jobs) if jobs else []

    return _FakeSource


# --- should_sweep -----------------------------------------------------------


def test_should_sweep_refuses_when_run_not_completed():
    decision = should_sweep(run_status="partial", candidates_discovered=5, jobs_failed=0)
    assert decision.performed is False
    assert decision.reason
    assert "partial" in decision.reason


def test_should_sweep_refuses_on_empty_board():
    decision = should_sweep(run_status="completed", candidates_discovered=0, jobs_failed=0)
    assert decision.performed is False
    assert decision.reason
    assert "empty board" in decision.reason or "no jobs" in decision.reason


def test_should_sweep_refuses_over_failure_ratio():
    decision = should_sweep(
        run_status="completed", candidates_discovered=10, jobs_failed=5, max_failure_ratio=0.25
    )
    assert decision.performed is False
    assert decision.reason
    assert "incomplete" in decision.reason or "failed" in decision.reason


def test_should_sweep_performs_on_clean_run():
    decision = should_sweep(run_status="completed", candidates_discovered=10, jobs_failed=1)
    assert decision.performed is True


# --- close_missing_jobs ------------------------------------------------------


def test_close_missing_jobs_closes_unobserved_active_jobs(db_session):
    kept = make_job_row(db_session, "kept-1")
    missing = make_job_row(db_session, "missing-1")
    db_session.commit()

    closed_count = close_missing_jobs(
        db_session,
        source=JobSourceType.GREENHOUSE,
        source_identifier="acme",
        observed_job_ids={kept.id},
        commit=True,
    )

    assert closed_count == 1
    db_session.refresh(kept)
    db_session.refresh(missing)
    assert kept.job_status == "ACTIVE"
    assert missing.job_status == "CLOSED"
    assert missing.closed_at is not None


def test_close_missing_jobs_does_not_touch_other_company_board(db_session):
    # source_job_id must differ across boards: (source, source_job_id) is
    # globally unique regardless of source_identifier.
    acme_job = make_job_row(db_session, "acme-j1", source_identifier="acme")
    other_job = make_job_row(db_session, "other-j1", source_identifier="other-co")
    db_session.commit()

    close_missing_jobs(
        db_session,
        source=JobSourceType.GREENHOUSE,
        source_identifier="acme",
        observed_job_ids=set(),
        commit=True,
    )

    db_session.refresh(acme_job)
    db_session.refresh(other_job)
    assert acme_job.job_status == "CLOSED"
    assert other_job.job_status == "ACTIVE"


def test_close_missing_jobs_does_not_touch_other_source(db_session):
    gh_job = make_job_row(db_session, "j1", source=JobSourceType.GREENHOUSE, source_identifier="acme")
    lever_job = make_job_row(db_session, "j1", source=JobSourceType.LEVER, source_identifier="acme")
    db_session.commit()

    close_missing_jobs(
        db_session,
        source=JobSourceType.GREENHOUSE,
        source_identifier="acme",
        observed_job_ids=set(),
        commit=True,
    )

    db_session.refresh(gh_job)
    db_session.refresh(lever_job)
    assert gh_job.job_status == "CLOSED"
    assert lever_job.job_status == "ACTIVE"


def test_close_missing_jobs_ignores_already_closed_jobs(db_session):
    original_closed_at = db_now() - timedelta(days=10)
    already_closed = make_job_row(
        db_session, "old-closed", status=JobStatus.CLOSED, closed_at=original_closed_at
    )
    db_session.commit()

    closed_count = close_missing_jobs(
        db_session,
        source=JobSourceType.GREENHOUSE,
        source_identifier="acme",
        observed_job_ids=set(),
        commit=True,
    )

    assert closed_count == 0
    db_session.refresh(already_closed)
    assert already_closed.closed_at == original_closed_at


def test_reopening_reactivates_closed_job(db_session):
    dedup = JobDeduplicator()
    job = make_normalized_job(source_job_id="201", content_hash="hash_v1")
    dedup.process(db_session, job)

    stored = db_session.query(JobRow).filter_by(source_job_id="201").first()
    stored.job_status = "CLOSED"
    stored.closed_at = db_now()
    db_session.commit()

    # The job reappeared on the board: same identity, same content.
    reappeared = make_normalized_job(source_job_id="201", content_hash="hash_v1")
    dedup.process(db_session, reappeared)

    db_session.refresh(stored)
    assert stored.job_status == "ACTIVE"
    assert stored.closed_at is None


# --- end-to-end through JobDiscoveryService ---------------------------------


@pytest.mark.asyncio
async def test_discovery_service_closes_job_missing_from_second_run(db_session):
    service = JobDiscoveryService()
    all_three = [make_raw_job("job-1"), make_raw_job("job-2"), make_raw_job("job-3")]
    service.register_source(FAKE_SOURCE_TYPE, make_fake_source(jobs=all_three))

    run1 = await service.run_discovery(db_session, FAKE_SOURCE_TYPE, "acme", company_name="Acme")
    assert run1.jobs_new == 3
    assert run1.jobs_closed == 0

    # job-3 vanished from the board on the second run.
    service.register_source(
        FAKE_SOURCE_TYPE, make_fake_source(jobs=[make_raw_job("job-1"), make_raw_job("job-2")])
    )
    run2 = await service.run_discovery(db_session, FAKE_SOURCE_TYPE, "acme", company_name="Acme")

    assert run2.jobs_closed == 1
    vanished = db_session.query(JobRow).filter_by(source_job_id="job-3").first()
    assert vanished.job_status == "CLOSED"
    assert vanished.closed_at is not None
    survivors = (
        db_session.query(JobRow)
        .filter(JobRow.source_job_id.in_(["job-1", "job-2"]))
        .all()
    )
    assert all(job.job_status == "ACTIVE" for job in survivors)


@pytest.mark.asyncio
async def test_discovery_service_source_failure_closes_nothing(db_session):
    existing = make_job_row(db_session, "job-1", source=JobSourceType.OTHER, source_identifier="acme")
    db_session.commit()

    error = SourceError("board unreachable", source=FAKE_SOURCE_TYPE, identifier="acme", status_code=500)
    service = JobDiscoveryService()
    service.register_source(FAKE_SOURCE_TYPE, make_fake_source(error=error))

    run = await service.run_discovery(db_session, FAKE_SOURCE_TYPE, "acme", company_name="Acme")

    assert run.status == "failed"
    assert (run.jobs_closed or 0) == 0
    db_session.refresh(existing)
    assert existing.job_status == "ACTIVE"
