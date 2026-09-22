"""Resumable runs, checkpoints, and source health / polling back-off."""

from datetime import timedelta

import pytest

from app.core.timeutils import db_now
from app.jobs.database.models import DiscoveryRunRow
from app.jobs.models.enums import JobSourceType
from app.jobs.pipeline.discovery_service import JobDiscoveryService
from app.jobs.pipeline.run_recovery import reconcile_stale_runs
from app.jobs.pipeline.source_health import SourceHealthRepository
from app.jobs.sources.base import SourceError
from tests.discovery.conftest import fake_source, make_raw


@pytest.mark.asyncio
async def test_resume_skips_what_the_interrupted_run_already_observed(db_session):
    service = JobDiscoveryService()
    first_batch = [make_raw("a"), make_raw("b", title="Data Engineer")]
    service.register_source(JobSourceType.OTHER, fake_source(jobs=first_batch))
    interrupted = await service.run_discovery(db_session, JobSourceType.OTHER, "acme", trigger="test")
    assert interrupted.checkpoint == {"processed": 2, "of": 2, "terminal": True}

    # Simulate the crash: the run row is stuck at running and gets reconciled.
    interrupted.status = "running"
    interrupted.started_at = db_now() - timedelta(minutes=120)
    db_session.commit()
    assert reconcile_stale_runs(db_session, timeout_minutes=60) == 1
    assert db_session.get(DiscoveryRunRow, interrupted.id).status == "interrupted"

    service.register_source(JobSourceType.OTHER, fake_source(jobs=first_batch + [make_raw("c", title="QA Engineer")]))
    resumed = await service.run_discovery(
        db_session, JobSourceType.OTHER, "acme", trigger="resume", resume_from_run_id=interrupted.id
    )
    assert resumed.resumed_from_run_id == interrupted.id
    assert resumed.jobs_resumed_skipped == 2 and resumed.jobs_new == 1 and resumed.jobs_unchanged == 0
    assert resumed.jobs_closed == 0, "resumed-skipped jobs count as observed for the sweep"

    unknown = await service.run_discovery(db_session, JobSourceType.OTHER, "acme", trigger="resume", resume_from_run_id="nope")
    assert unknown.jobs_resumed_skipped == 0 and unknown.jobs_unchanged == 3


@pytest.mark.asyncio
async def test_source_health_tracks_success_failure_and_backoff(db_session, monkeypatch):
    monkeypatch.setattr("app.config.settings.discovery_default_poll_minutes", 60)
    service = JobDiscoveryService()
    service.register_source(JobSourceType.OTHER, fake_source(jobs=[make_raw("a", posted_at=db_now() - timedelta(days=1))]))
    await service.run_discovery(db_session, JobSourceType.OTHER, "acme", company_name="Acme", trigger="test")
    health = SourceHealthRepository(db_session)
    row = health.get(JobSourceType.OTHER, "acme")
    assert row.runs_total == 1 and row.runs_success == 1 and row.consecutive_failures == 0
    assert row.company_name == "Acme" and row.last_jobs_new == 1 and row.jobs_fetched_total == 1
    assert row.last_fresh_posted_at is not None
    assert SourceHealthRepository.success_rate(row) == 1.0
    assert row.next_poll_at > db_now() + timedelta(minutes=55)
    assert health.due() == [], "just polled: not due"

    service.register_source(JobSourceType.OTHER, fake_source(error=SourceError("429", JobSourceType.OTHER, "acme", 429, kind="rate_limited")))
    for expected_failures in (1, 2):
        await service.run_discovery(db_session, JobSourceType.OTHER, "acme", trigger="test")
        row = health.get(JobSourceType.OTHER, "acme")
        assert row.consecutive_failures == expected_failures
    assert row.runs_failed == 2 and row.runs_rate_limited == 2 and row.last_failure_kind == "rate_limited"
    assert "429" in row.last_error
    assert row.next_poll_at >= db_now() + timedelta(minutes=60 * 4 - 1), "interval doubled per failure"
    assert row.enabled, "never auto-disabled"
    assert SourceHealthRepository.success_rate(row) == round(1 / 3, 3)

    row.next_poll_at = db_now() - timedelta(minutes=1)
    db_session.commit()
    assert [r.source_identifier for r in health.due()] == ["acme"]
    health.upsert_target(JobSourceType.OTHER, "acme", enabled=False)
    db_session.commit()
    assert health.due() == []

    service.register_source(JobSourceType.OTHER, fake_source(jobs=[make_raw("a")]))
    await service.run_discovery(db_session, JobSourceType.OTHER, "acme", trigger="test")
    row = health.get(JobSourceType.OTHER, "acme")
    assert row.consecutive_failures == 0 and row.last_failure_kind is None and row.runs_success == 2
