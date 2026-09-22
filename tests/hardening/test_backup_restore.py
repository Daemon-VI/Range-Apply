"""Backup / restore of a representative synthetic database: a migrated
SQLite file with a tenant, policy, attempt, run, signal, outcome and learning
snapshot is copied (the documented procedure), the copy is opened as the
configured database, `alembic upgrade head` is a no-op, the schema verifies,
every row and every invariant survives, and startup recovery runs cleanly."""

import shutil
import uuid

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.database import verify_schema
from tests.test_migrations import _alembic_config, _pointed_at, _sqlite_url


def _populate(url: str) -> dict:
    from app.application.database.models import ApplicationRow
    from app.career.database.models import TenantRow
    from app.execution.database.models import ExecutionRunRow
    from app.jobs.database.models import JobRow
    from app.learning.database.models import LearningSnapshotRow
    from app.pipeline.database.models import (
        ApplicationPolicyRow,
        CandidateOpportunityRow,
        OpportunityRow,
    )
    from app.signals.database.models import OutcomeEventRow, SignalRow

    engine = create_engine(url)
    session = sessionmaker(bind=engine)()
    try:
        tid = "backup-" + uuid.uuid4().hex[:6]
        session.add(TenantRow(id=tid, name="backup"))
        session.add(ApplicationPolicyRow(tenant_id=tid, enabled_bands=["HIGH", "MEDIUM", "LOW"], band_thresholds={"HIGH": 70, "MEDIUM": 45}, tailoring_by_band={}, lane_by_band={}))
        job = JobRow(canonical_key="bk-1", source="GREENHOUSE", source_job_id="bk-1", company="Backup Co", title="Engineer", original_title="Engineer", source_url="https://x/bk-1", content_hash="h1")
        session.add(job)
        session.flush()
        opp = OpportunityRow(identity_key="bk-1", canonical_job_id=job.id, company="Backup Co", company_key="backup", title="Engineer")
        session.add(opp)
        session.flush()
        co = CandidateOpportunityRow(tenant_id=tid, opportunity_id=opp.id, state="SUBMITTED")
        session.add(co)
        session.flush()
        attempt = ApplicationRow(job_id=job.id, tenant_id=tid, opportunity_id=opp.id, candidate_opportunity_id=co.id, status="UNCERTAIN", attempt_number=1, submission_key=f"{tid}:a:1:1")
        session.add(attempt)
        session.flush()
        run = ExecutionRunRow(tenant_id=tid, application_id=attempt.id, executor_kind="MOCK", executor_version="v", idempotency_key=f"{tid}:a:1:1", run_number=1, status="UNKNOWN", outcome="UNKNOWN", submit_invoked=True)
        session.add(run)
        signal = SignalRow(tenant_id=tid, source="EMAIL", source_reference="<bk@x>", content_hash="c", dedupe_key="EMAIL:ref:<bk@x>", status="APPLIED", application_id=attempt.id)
        session.add(signal)
        session.flush()
        session.add(OutcomeEventRow(tenant_id=tid, application_id=attempt.id, signal_id=signal.id, outcome="APPLICATION_RECEIVED", evidence="STRONG", origin="rules", sequence=1, dedupe_key="bk-event", rules_version="outcome-rules-v1"))
        session.add(LearningSnapshotRow(tenant_id=tid, learning_version="learning-v1", feature_version="features-v1", smoothing_method="s", outcome_rules_version="o", attribution_version="a", as_of=__import__("app.core.timeutils", fromlist=["db_now"]).db_now(), dataset_size=1))
        session.commit()
        return {"tenant": tid, "attempt": attempt.id, "run": run.id, "signal": signal.id}
    finally:
        session.close()
        engine.dispose()


def test_backup_copy_restores_every_row_and_invariant(tmp_path):
    original = tmp_path / "careeros.db"
    with _pointed_at(_sqlite_url(original)):
        command.upgrade(_alembic_config(), "head")
    ids = _populate(_sqlite_url(original))
    backup = tmp_path / "careeros-backup.db"
    shutil.copyfile(original, backup)  # the documented procedure (or sqlite3 .backup)
    restored_url = _sqlite_url(backup)
    with _pointed_at(restored_url):
        command.upgrade(_alembic_config(), "head")  # a no-op on a current backup
        engine = create_engine(restored_url)
        try:
            verify_schema(engine)
            inspector = inspect(engine)
            assert "alembic_version" in inspector.get_table_names()
            session = sessionmaker(bind=engine)()
            try:
                from app.application.database.models import ApplicationRow
                from app.execution.database.models import ExecutionRunRow
                from app.execution.service import ExecutionService
                from app.learning.database.models import LearningSnapshotRow
                from app.signals.database.models import OutcomeEventRow, SignalRow

                assert session.get(ApplicationRow, ids["attempt"]).status == "UNCERTAIN"
                assert session.get(ExecutionRunRow, ids["run"]).submit_invoked is True
                assert session.get(SignalRow, ids["signal"]).status == "APPLIED"
                assert session.query(OutcomeEventRow).filter_by(application_id=ids["attempt"]).count() == 1
                assert session.query(LearningSnapshotRow).filter_by(tenant_id=ids["tenant"]).count() == 1
                # startup recovery on the restored database: the UNKNOWN run stays UNKNOWN, nothing is retried
                counts = ExecutionService(session, ids["tenant"], actor="restore", executors={}).recover_lost_runs()
                assert counts == {} and session.get(ApplicationRow, ids["attempt"]).status == "UNCERTAIN"
                # invariants still enforced after restore
                import pytest
                from sqlalchemy.exc import IntegrityError

                session.add(SignalRow(tenant_id=ids["tenant"], source="EMAIL", content_hash="c2", dedupe_key="EMAIL:ref:<bk@x>", status="NEW"))
                with pytest.raises(IntegrityError):
                    session.flush()
                session.rollback()
            finally:
                session.close()
        finally:
            engine.dispose()
