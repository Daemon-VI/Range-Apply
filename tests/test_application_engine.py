"""Tests for the Phase 5 application engine + Phase 6-lite kill switch.

Uses its own in-memory SQLite engine with ``JobRow`` plus the application
tables. ``prepare()`` needs an approved tailoring artifact, but that table is
owned by a concurrently-built module (``app.tailoring``), so this test creates
a minimal ``tailored_artifacts`` table itself via raw SQL matching the frozen
schema, fully decoupling these tests from that module's build timing.
"""

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.application.database.models import ApplicationEventRow, ApplicationRow
from app.application.killswitch import is_paused, set_paused
from app.application.models import ApplicationStatus
from app.jobs.database.models import Base, JobRow


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    # Stand-in for the tailoring module's table, matching its frozen contract
    # (app/tailoring/database/models.py) so this test stays decoupled from
    # that module's own build/import timing. IF NOT EXISTS because a prior
    # test in this process may already have imported the real ORM class,
    # registering it (with the same shape) on the shared Base metadata.
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS tailored_artifacts (
                    id VARCHAR(36) PRIMARY KEY,
                    job_id VARCHAR(36) NOT NULL,
                    match_id VARCHAR(36),
                    artifact_type VARCHAR(32) NOT NULL,
                    version INTEGER DEFAULT 1,
                    title VARCHAR(256),
                    content TEXT,
                    evidence_refs JSON,
                    template_name VARCHAR(64),
                    approved BOOLEAN NOT NULL DEFAULT 0,
                    created_at DATETIME
                )
                """
            )
        )
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _make_job(db_session, source="GREENHOUSE") -> str:
    job_id = str(uuid.uuid4())
    job = JobRow(
        id=job_id,
        canonical_key=f"key-{job_id}",
        source=source,
        source_job_id="1",
        company="Acme",
        title="Engineer",
        original_title="Engineer",
        source_url="https://boards.greenhouse.io/acme/jobs/1",
        application_url="https://boards.greenhouse.io/acme/jobs/1",
        content_hash="abc",
    )
    db_session.add(job)
    db_session.commit()
    return job_id


def _approve_artifact(db_session, job_id: str) -> None:
    db_session.execute(
        text(
            "INSERT INTO tailored_artifacts (id, job_id, artifact_type, approved) "
            "VALUES (:id, :job_id, 'RESUME', 1)"
        ),
        {"id": str(uuid.uuid4()), "job_id": job_id},
    )
    db_session.commit()


def test_get_or_create_creates_at_discovered(db_session):
    from app.application.engine import ApplicationEngine

    job_id = _make_job(db_session)
    engine = ApplicationEngine()

    row = engine.get_or_create(db_session, job_id)

    assert row.status == ApplicationStatus.DISCOVERED.value
    assert row.job_id == job_id

    # Idempotent lookup - no duplicate row.
    again = engine.get_or_create(db_session, job_id)
    assert again.id == row.id


def test_prepare_without_approved_artifact_raises(db_session):
    from app.application.engine import ApplicationEngine

    job_id = _make_job(db_session)
    engine = ApplicationEngine()

    with pytest.raises(ValueError):
        engine.prepare(db_session, job_id)


def test_prepare_with_approved_artifact_advances_to_ready(db_session):
    from app.application.engine import ApplicationEngine

    job_id = _make_job(db_session)
    _approve_artifact(db_session, job_id)
    engine = ApplicationEngine()

    row = engine.prepare(db_session, job_id)

    assert row.status == ApplicationStatus.READY.value
    assert len(row.tailored_artifact_ids) == 1

    events = db_session.query(ApplicationEventRow).filter_by(application_id=row.id).all()
    assert any(e.to_status == ApplicationStatus.READY.value for e in events)


@pytest.mark.asyncio
async def test_submit_without_approval_raises(db_session):
    from app.application.engine import ApplicationEngine

    job_id = _make_job(db_session)
    _approve_artifact(db_session, job_id)
    engine = ApplicationEngine()
    engine.prepare(db_session, job_id)

    with pytest.raises(ValueError):
        await engine.submit(db_session, job_id, approved=False, dry_run=True)


@pytest.mark.asyncio
async def test_submit_blocked_by_global_killswitch(db_session):
    from app.application.engine import ApplicationEngine

    job_id = _make_job(db_session)
    _approve_artifact(db_session, job_id)
    engine = ApplicationEngine()
    engine.prepare(db_session, job_id)
    set_paused(db_session, paused=True, reason="testing", source=None)

    with pytest.raises(RuntimeError):
        await engine.submit(db_session, job_id, approved=True, dry_run=True)

    row = db_session.query(ApplicationRow).filter_by(job_id=job_id).first()
    assert row.status == ApplicationStatus.READY.value  # unchanged

    # Cleanup so other tests in this module aren't affected.
    set_paused(db_session, paused=False, source=None)


@pytest.mark.asyncio
async def test_submit_dry_run_succeeds_without_playwright(db_session):
    from app.application.engine import ApplicationEngine

    job_id = _make_job(db_session)
    _approve_artifact(db_session, job_id)
    engine = ApplicationEngine()
    engine.prepare(db_session, job_id)

    row = await engine.submit(db_session, job_id, approved=True, dry_run=True)

    # Phase 7: a dry run on the legacy route records an event and changes
    # nothing; it never marks the attempt SUBMITTED.
    assert row.status == ApplicationStatus.READY.value
    assert row.submitted_at is None and row.dry_run is True
    events = db_session.query(ApplicationEventRow).filter_by(application_id=row.id).all()
    assert any(e.event_type == "legacy_dry_run" for e in events)


@pytest.mark.asyncio
async def test_legacy_live_submit_is_refused(db_session):
    """The v5 route cannot bypass the execution service's safety gates."""
    from app.application.engine import ApplicationEngine

    job_id = _make_job(db_session)
    _approve_artifact(db_session, job_id)
    engine = ApplicationEngine()
    engine.prepare(db_session, job_id)

    with pytest.raises(RuntimeError, match="/api/v1/execution"):
        await engine.submit(db_session, job_id, approved=True, dry_run=False)
    row = db_session.query(ApplicationRow).filter_by(job_id=job_id).first()
    assert row.status == ApplicationStatus.READY.value
    assert db_session.query(ApplicationEventRow).filter_by(application_id=row.id, event_type="legacy_submit_refused").count() == 1


@pytest.mark.asyncio
async def test_submit_twice_is_idempotent(db_session):
    from app.application.engine import ApplicationEngine

    job_id = _make_job(db_session)
    _approve_artifact(db_session, job_id)
    engine = ApplicationEngine()
    engine.prepare(db_session, job_id)
    first = await engine.submit(db_session, job_id, approved=True, dry_run=True)
    # An attempt that was really submitted (through the execution service) is
    # returned untouched by the legacy route, with no new events.
    first.status = ApplicationStatus.SUBMITTED.value
    db_session.commit()
    events_after_first = db_session.query(ApplicationEventRow).filter_by(application_id=first.id).count()

    second = await engine.submit(db_session, job_id, approved=True, dry_run=True)
    events_after_second = db_session.query(ApplicationEventRow).filter_by(application_id=first.id).count()

    assert second.id == first.id
    assert second.status == ApplicationStatus.SUBMITTED.value
    assert events_after_second == events_after_first
    assert db_session.query(ApplicationRow).filter_by(job_id=job_id).count() == 1


def test_killswitch_checks_global_and_source(db_session):
    _make_job(db_session, source="GREENHOUSE")

    assert is_paused(db_session, "GREENHOUSE") is False

    set_paused(db_session, paused=True, reason="incident", source="GREENHOUSE")
    assert is_paused(db_session, "GREENHOUSE") is True
    assert is_paused(db_session, "LEVER") is False

    set_paused(db_session, paused=False, source="GREENHOUSE")
    assert is_paused(db_session, "GREENHOUSE") is False

    set_paused(db_session, paused=True, reason="global freeze", source=None)
    assert is_paused(db_session, "LEVER") is True
    set_paused(db_session, paused=False, source=None)


def test_greenhouse_adapter_module_imports_without_playwright():
    # Must not raise even though playwright is not installed in this env.
    import app.application.adapters.greenhouse_playwright as module

    assert hasattr(module, "GreenhousePlaywrightAdapter")
