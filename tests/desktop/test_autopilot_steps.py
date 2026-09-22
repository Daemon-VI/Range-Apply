"""Autopilot's real steps against the test database (no network, no browser)."""

from app.application.database.models import ApplicationRow
from app.database import get_session_factory
from app.desktop import autopilot
from app.jobs.pipeline.source_health import SourceHealthRepository
from tests.scheduler.conftest import (  # noqa: F401
    answered_bank,
    db_session,
    evidence,
    jobs,
    matches,
    opportunities,
    scheduler,
    tenant_id,
)


def test_discover_with_no_due_boards_does_nothing(monkeypatch, tenant_id):  # noqa: F811
    monkeypatch.setattr(SourceHealthRepository, "due", lambda self, now=None, limit=50: [])
    assert autopilot.discover_step(tenant_id, get_session_factory()) == "no boards due"


def test_match_with_no_changed_jobs_does_nothing(monkeypatch, tenant_id):  # noqa: F811
    import app.intelligence.services.match_persistence as persistence

    monkeypatch.setattr(persistence, "stale_job_ids", lambda db, run_id=None: [])
    assert autopilot.match_step(tenant_id, get_session_factory()) == "no changed jobs"


def test_prepare_admits_and_prepares_through_the_scheduler(db_session, tenant_id, opportunities, answered_bank):  # noqa: F811
    co = opportunities.make(title="Backend Engineer", company="Autopilot Co", fit_score=90)
    db_session.commit()
    detail = autopilot.prepare_step(tenant_id, get_session_factory())
    assert detail.startswith("scheduler "), detail
    db_session.expire_all()
    attempt = db_session.query(ApplicationRow).filter(ApplicationRow.tenant_id == tenant_id, ApplicationRow.opportunity_id == co.opportunity_id).first()
    assert attempt is not None, "the scheduler pass created the attempt"
    assert attempt.status not in ("SUBMITTED", "VERIFIED", "UNCERTAIN", "SUBMITTING"), "autopilot never submits"


def test_a_full_cycle_with_real_steps_never_submits(monkeypatch, db_session, tenant_id, opportunities, answered_bank):  # noqa: F811
    import app.intelligence.services.match_persistence as persistence

    monkeypatch.setattr(SourceHealthRepository, "due", lambda self, now=None, limit=50: [])
    monkeypatch.setattr(persistence, "stale_job_ids", lambda db, run_id=None: [])
    opportunities.make(title="Platform Engineer", company="Cycle Co", fit_score=88)
    db_session.commit()
    pilot = autopilot.Autopilot(tenant_id, session_factory=get_session_factory())
    results = pilot.run_once()
    assert list(results) == ["discover", "match", "prepare"] and all(r["ok"] for r in results.values()), results
    db_session.expire_all()
    assert db_session.query(ApplicationRow).filter(ApplicationRow.tenant_id == tenant_id, ApplicationRow.status.in_(["SUBMITTED", "VERIFIED", "UNCERTAIN", "SUBMITTING"])).count() == 0


def test_match_rescores_every_open_job_when_an_open_job_changed(monkeypatch, db_session, tenant_id, opportunities, answered_bank):  # noqa: F811
    import app.intelligence.services.match_persistence as persistence
    from app.intelligence.database.models import JobMatchRow, MatchRunRow
    from app.jobs.database.models import JobRow

    co = opportunities.make(title="Backend Engineer", company="Rematch Co", fit_score=80)
    other = opportunities.make(title="Platform Engineer", company="Unchanged Co", fit_score=70)
    db_session.commit()
    changed, unchanged = co.opportunity.canonical_job_id, other.opportunity.canonical_job_id
    monkeypatch.setattr(persistence, "stale_job_ids", lambda db, run_id=None: [changed])
    before = db_session.query(MatchRunRow).count()
    detail = autopilot.match_step(tenant_id, get_session_factory())
    assert detail.startswith("1 changed job(s): re-scored "), detail
    db_session.expire_all()
    run = db_session.query(MatchRunRow).order_by(MatchRunRow.started_at.desc()).first()
    assert db_session.query(MatchRunRow).count() == before + 1 and run.trigger == "autopilot" and run.tenant_id == tenant_id
    open_jobs = db_session.query(JobRow).filter(JobRow.job_status != "CLOSED").count()
    assert run.jobs_processed == open_jobs, "a full run: the latest run must never hide other jobs' scores"
    scored = {m.job_id for m in db_session.query(JobMatchRow).filter(JobMatchRow.run_id == run.id)}
    assert {changed, unchanged} <= scored


def test_closed_jobs_that_look_changed_never_create_an_empty_run(monkeypatch, db_session, tenant_id, opportunities, answered_bank):  # noqa: F811
    import app.intelligence.services.match_persistence as persistence
    from app.intelligence.database.models import MatchRunRow
    from app.jobs.database.models import JobRow

    co = opportunities.make(title="Closed Role", company="Closed Co", fit_score=60)
    db_session.commit()
    job = db_session.get(JobRow, co.opportunity.canonical_job_id)
    job.job_status = "CLOSED"
    db_session.commit()
    monkeypatch.setattr(persistence, "stale_job_ids", lambda db, run_id=None: [job.id])
    before = db_session.query(MatchRunRow).count()
    assert autopilot.match_step(tenant_id, get_session_factory()) == "no changed jobs"
    db_session.expire_all()
    assert db_session.query(MatchRunRow).count() == before, "no run at all"


def test_match_never_passes_a_subset_of_jobs(monkeypatch, db_session, tenant_id, opportunities, answered_bank):  # noqa: F811
    import app.intelligence.services.match_persistence as persistence
    import app.pipeline.sync as sync

    co = opportunities.make(title="Backend Engineer", company="Subset Co", fit_score=80)
    db_session.commit()
    calls = {}

    def fake_run(**kwargs):
        calls.update(kwargs)
        from types import SimpleNamespace

        return SimpleNamespace(id="run-x", jobs_processed=3)

    monkeypatch.setattr(persistence, "stale_job_ids", lambda db, run_id=None: [co.opportunity.canonical_job_id])
    monkeypatch.setattr(persistence, "run_matching", fake_run)
    monkeypatch.setattr(sync, "sync_run", lambda db, tenant, run_id, actor=None: None)
    assert autopilot.match_step(tenant_id, get_session_factory()) == "1 changed job(s): re-scored 3 open job(s)"
    assert calls["job_ids"] is None and calls["trigger"] == "autopilot" and calls["tenant_id"] == tenant_id
