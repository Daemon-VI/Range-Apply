"""Tests for ingestion transaction safety and discovery run status.

Exercises :class:`JobDiscoveryService` against a fake, in-process
``JobSource`` (registered via ``register_source``) so no network is touched.
The fake returns hand-built ``RawJob`` instances, and failures are injected by
wrapping the normalizer or deduplicator, matching how the pipeline itself
would encounter a malformed job or a transient database error.
"""

from datetime import timedelta
from typing import List, Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.core.timeutils import db_now
from app.jobs.database.models import Base, DiscoveryRunRow, JobRow
from app.jobs.deduplication.deduplicator import JobDeduplicator
from app.jobs.models.enums import JobSourceType
from app.jobs.models.raw_job import RawJob
from app.jobs.normalization.normalizer import JobNormalizer
from app.jobs.pipeline.discovery_service import JobDiscoveryService
from app.jobs.pipeline.run_recovery import reconcile_stale_runs
from app.jobs.sources.base import JobSource, SourceError

# All fakes register under OTHER so real GREENHOUSE/LEVER/ASHBY adapters (and
# their network calls) are never involved.
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


def make_raw_job(source_job_id: str, identifier: str = "acme", title: Optional[str] = None) -> RawJob:
    """Builds a well-formed RawJob for a fake board."""
    title = title or f"Backend Engineer {source_job_id}"
    return RawJob(
        source=FAKE_SOURCE_TYPE,
        source_job_id=source_job_id,
        source_url=f"https://example.com/{identifier}/jobs/{source_job_id}",
        discovered_url=f"https://example.com/{identifier}/jobs/{source_job_id}",
        raw_title=title,
        raw_content="<p>Looking for someone with Python and SQL experience.</p>",
        raw_location="Remote",
        raw_metadata={"board_token": identifier},
    )


def make_fake_source(jobs: Optional[List[RawJob]] = None, error: Optional[Exception] = None) -> type:
    """Builds a JobSource subclass whose ``discover`` returns fixed data (or raises).

    A fresh class is returned per call because ``register_source`` stores a
    *type*, which the service later instantiates with no arguments — a plain
    closure over test data would not survive that instantiation.
    """

    class _FakeSource(JobSource):
        @property
        def source_type(self) -> JobSourceType:
            return FAKE_SOURCE_TYPE

        async def discover(self, identifier: str, **kwargs) -> List[RawJob]:
            if error is not None:
                raise error
            return list(jobs) if jobs else []

    return _FakeSource


class NormalizerFailingFor(JobNormalizer):
    """A real normalizer that raises for one chosen source_job_id.

    ``times`` bounds how many times it fails before delegating normally,
    so it can model either a permanent malformed job or a transient one
    that succeeds on retry.
    """

    def __init__(self, fail_source_job_id: str, times: int = 1_000_000):
        super().__init__()
        self.fail_source_job_id = fail_source_job_id
        self.times = times
        self.failures = 0

    def normalize(self, raw_job: RawJob, company_name: Optional[str] = None):
        if raw_job.source_job_id == self.fail_source_job_id and self.failures < self.times:
            self.failures += 1
            raise ValueError(f"malformed payload for {raw_job.source_job_id}")
        return super().normalize(raw_job, company_name=company_name)


class DeduplicatorFailingFor(JobDeduplicator):
    """A real deduplicator that raises SQLAlchemyError once for one job.

    Used to prove the per-job SAVEPOINT actually protects the session: a
    simulated DB error must not stop later jobs in the same run from
    persisting.
    """

    def __init__(self, fail_source_job_id: str):
        super().__init__()
        self.fail_source_job_id = fail_source_job_id
        self._failed = False

    def process(self, db, job, commit: bool = True):
        if job.source_job_id == self.fail_source_job_id and not self._failed:
            self._failed = True
            raise SQLAlchemyError("simulated database error")
        return super().process(db, job, commit=commit)


@pytest.mark.asyncio
async def test_malformed_job_completes_run_as_partial(db_session):
    jobs = [make_raw_job("job-1"), make_raw_job("bad-job"), make_raw_job("job-3")]
    service = JobDiscoveryService(normalizer=NormalizerFailingFor("bad-job"))
    service.register_source(FAKE_SOURCE_TYPE, make_fake_source(jobs=jobs))

    run = await service.run_discovery(db_session, FAKE_SOURCE_TYPE, "acme", company_name="Acme")

    assert run.status == "partial"
    assert run.jobs_failed == 1
    assert run.jobs_new == 2
    assert db_session.query(JobRow).count() == 2
    assert any("bad-job" in err for err in run.errors), run.errors


@pytest.mark.asyncio
async def test_db_error_mid_run_does_not_poison_later_jobs(db_session):
    jobs = [make_raw_job("job-a"), make_raw_job("job-b"), make_raw_job("job-c")]
    service = JobDiscoveryService(deduplicator=DeduplicatorFailingFor("job-b"))
    service.register_source(FAKE_SOURCE_TYPE, make_fake_source(jobs=jobs))

    run = await service.run_discovery(db_session, FAKE_SOURCE_TYPE, "acme", company_name="Acme")

    assert run.status == "partial"
    assert run.jobs_failed == 1
    # job-a persisted before the error and job-c persisted after it: the
    # SAVEPOINT rollback left the session usable for the rest of the run.
    assert db_session.query(JobRow).filter_by(source_job_id="job-a").count() == 1
    assert db_session.query(JobRow).filter_by(source_job_id="job-c").count() == 1
    assert db_session.query(JobRow).filter_by(source_job_id="job-b").count() == 0


@pytest.mark.asyncio
async def test_source_adapter_failure_marks_run_failed_not_running(db_session):
    error = SourceError("board unreachable", source=FAKE_SOURCE_TYPE, identifier="acme", status_code=500)
    service = JobDiscoveryService()
    service.register_source(FAKE_SOURCE_TYPE, make_fake_source(error=error))

    run = await service.run_discovery(db_session, FAKE_SOURCE_TYPE, "acme", company_name="Acme")

    assert run.status == "failed"
    assert run.status != "running"
    assert len(run.errors) > 0
    assert any("board unreachable" in err for err in run.errors)


@pytest.mark.asyncio
async def test_retry_after_failure_is_idempotent_and_reports_duplicates(db_session):
    jobs = [make_raw_job("job-1"), make_raw_job("job-2"), make_raw_job("job-3")]
    # Fails exactly once: the first run hits the failure, the second (a
    # retry over the same board) succeeds for every job.
    normalizer = NormalizerFailingFor("job-2", times=1)
    service = JobDiscoveryService(normalizer=normalizer)
    service.register_source(FAKE_SOURCE_TYPE, make_fake_source(jobs=jobs))

    run1 = await service.run_discovery(db_session, FAKE_SOURCE_TYPE, "acme", company_name="Acme")
    assert run1.status == "partial"
    assert run1.jobs_new == 2
    assert run1.jobs_failed == 1

    run2 = await service.run_discovery(db_session, FAKE_SOURCE_TYPE, "acme", company_name="Acme")
    assert run2.status == "completed"
    assert run2.jobs_failed == 0
    # job-2 is new this time (first successful sighting); the other two are
    # duplicates, not fresh rows.
    assert run2.jobs_new == 1
    assert run2.jobs_duplicate == 2

    # No duplicate JobRows were created across the two runs.
    assert db_session.query(JobRow).count() == 3


@pytest.mark.parametrize(
    ("scenario", "expected_status"),
    [("success", "completed"), ("partial", "partial"), ("failed", "failed")],
)
@pytest.mark.asyncio
async def test_run_status_is_always_terminal(db_session, scenario, expected_status):
    service = JobDiscoveryService()
    if scenario == "success":
        jobs = [make_raw_job("job-1"), make_raw_job("job-2")]
        service.register_source(FAKE_SOURCE_TYPE, make_fake_source(jobs=jobs))
    elif scenario == "partial":
        jobs = [make_raw_job("job-1"), make_raw_job("bad-job")]
        service = JobDiscoveryService(normalizer=NormalizerFailingFor("bad-job"))
        service.register_source(FAKE_SOURCE_TYPE, make_fake_source(jobs=jobs))
    else:
        error = SourceError("boom", source=FAKE_SOURCE_TYPE, identifier="acme", status_code=500)
        service.register_source(FAKE_SOURCE_TYPE, make_fake_source(error=error))

    run = await service.run_discovery(db_session, FAKE_SOURCE_TYPE, "acme", company_name="Acme")

    assert run.status == expected_status
    assert run.status != "running"
    assert run.completed_at is not None
    assert run.duration_seconds is not None


def test_reconcile_stale_runs_marks_old_running_row_interrupted(db_session):
    stale = DiscoveryRunRow(
        source="GREENHOUSE",
        source_identifier="acme",
        started_at=db_now() - timedelta(hours=2),
        status="running",
        errors=[],
        trigger="manual",
    )
    db_session.add(stale)
    db_session.commit()

    reconciled = reconcile_stale_runs(db_session, timeout_minutes=0)

    assert reconciled == 1
    db_session.refresh(stale)
    assert stale.status == "interrupted"
    assert stale.completed_at is not None
    assert len(stale.errors) == 1
    assert "interrupted" in stale.errors[0].lower()


def test_reconcile_stale_runs_leaves_fresh_running_row_alone(db_session):
    # started_at is set a little ahead of "now" so it is guaranteed to land
    # after reconcile's cutoff even with timeout_minutes=0, without relying
    # on sub-second timing between the insert and the reconcile call.
    fresh = DiscoveryRunRow(
        source="GREENHOUSE",
        source_identifier="acme",
        started_at=db_now() + timedelta(minutes=5),
        status="running",
        errors=[],
        trigger="manual",
    )
    db_session.add(fresh)
    db_session.commit()

    reconciled = reconcile_stale_runs(db_session, timeout_minutes=0)

    assert reconciled == 0
    db_session.refresh(fresh)
    assert fresh.status == "running"
    assert fresh.completed_at is None
