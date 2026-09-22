"""Migration review: fresh upgrade, downgrade one step and back, full
downgrade to base and back, head correctness, ORM parity (tables and
columns) after the round trips."""

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from alembic import command
from app.config import PROJECT_ROOT
from app.jobs.database.models import Base
from tests.test_migrations import _alembic_config, _pointed_at, _sqlite_url


def _head() -> str:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    assert len(heads) == 1, f"multiple heads: {heads}"
    return heads[0]


def _tables(url: str) -> dict[str, set[str]]:
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        return {t: {c["name"] for c in inspector.get_columns(t)} for t in inspector.get_table_names() if t != "alembic_version"}
    finally:
        engine.dispose()


def test_head_is_single_and_full_round_trips_keep_orm_parity(tmp_path):
    head = _head()
    assert head == "f2c6a4b8d1e7", "the 2026-09-22 reprepare counter is the current head"
    url = _sqlite_url(tmp_path / "chain.db")
    with _pointed_at(url):
        cfg = _alembic_config()
        command.upgrade(cfg, "head")
        fresh = _tables(url)
        command.downgrade(cfg, "-1")
        assert "reprepare_requeued" not in _tables(url)["scheduler_runs"], "one step back removes the 2026-09-22 counter only"
        command.upgrade(cfg, "head")
        assert _tables(url) == fresh
        command.downgrade(cfg, "base")
        assert _tables(url) == {}, "a full downgrade leaves an empty schema (no destructive leftovers)"
        command.upgrade(cfg, "head")
        after = _tables(url)
    assert after == fresh
    expected = {name: {c.name for c in table.columns} for name, table in Base.metadata.tables.items()}
    assert set(after) == set(expected)
    for name, cols in expected.items():
        assert after[name] == cols, f"column drift in {name}: {after[name] ^ cols}"


def test_critical_constraints_and_indexes_exist(tmp_path):
    url = _sqlite_url(tmp_path / "idx.db")
    with _pointed_at(url):
        command.upgrade(_alembic_config(), "head")
    engine = create_engine(url)
    try:
        inspector = inspect(engine)

        def uniques(table):
            return {tuple(u["column_names"]) for u in inspector.get_unique_constraints(table)} | {tuple(i["column_names"]) for i in inspector.get_indexes(table) if i.get("unique")}

        def indexes(table):
            return {tuple(i["column_names"]) for i in inspector.get_indexes(table)}

        assert ("tenant_id", "opportunity_id") in uniques("applications") and ("submission_key",) in uniques("applications")
        assert ("tenant_id", "opportunity_id") in uniques("candidate_opportunities")
        assert ("tenant_id", "opportunity_id", "action") in uniques("application_queue") and ("idempotency_key",) in uniques("application_queue")
        assert ("tenant_id", "state", "available_at", "priority") in indexes("application_queue")
        assert ("idempotency_key",) in uniques("execution_runs")
        assert ("tenant_id", "dedupe_key") in uniques("signals") and ("tenant_id", "dedupe_key") in uniques("outcome_events")
        assert ("tenant_id", "application_id") in uniques("application_outcomes")
        assert ("tenant_id", "period_kind", "period_key") in uniques("application_cap_ledger")
        assert ("canonical_key",) in uniques("jobs") and ("identity_key",) in uniques("opportunities") and ("job_id",) in uniques("opportunity_jobs")
        assert ("tenant_id", "key") in uniques("evidence_nodes")
        assert ("tenant_id", "generated_at") in indexes("learning_snapshots") and ("snapshot_id", "dimension", "metric") in indexes("learning_metrics")
        assert ("tenant_id", "application_id", "sequence") in indexes("outcome_events") and ("tenant_id", "application_id") in indexes("signals")
        assert ("tenant_id", "status") in indexes("execution_runs") and ("tenant_id", "status") in indexes("applications")
    finally:
        engine.dispose()
