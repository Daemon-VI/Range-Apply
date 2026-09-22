"""A match run a stopped process left RUNNING is reconciled, and never reads as "in progress"."""

import uuid
from datetime import timedelta

from app.core.timeutils import db_now
from app.database import get_session_factory
from app.intelligence.database.models import MatchRunRow
from app.intelligence.services.match_persistence import (
    latest_run,
    match_run_is_stale,
    reconcile_stale_match_runs,
)
from tests.scheduler.conftest import db_session, evidence, tenant_id  # noqa: F401


def _run(db, status, minutes_ago, tenant=None):
    row = MatchRunRow(id=str(uuid.uuid4()), policy_version="v1", engine_version="1.3.0", career_brain_version="v1", started_at=db_now() - timedelta(minutes=minutes_ago), status=status, trigger="manual", errors=[], tenant_id=tenant)
    db.add(row)
    db.commit()
    return row


def test_only_old_running_rows_are_marked_interrupted():
    db = get_session_factory()()
    try:
        old = _run(db, "RUNNING", 180)
        fresh = _run(db, "RUNNING", 2)
        done = _run(db, "COMPLETED", 300)
        assert match_run_is_stale(old) and not match_run_is_stale(fresh) and not match_run_is_stale(done)
        assert reconcile_stale_match_runs(db) >= 1
        db.expire_all()
        assert db.get(MatchRunRow, old.id).status == "FAILED" and "interrupted" in db.get(MatchRunRow, old.id).errors[-1]
        assert db.get(MatchRunRow, fresh.id).status == "RUNNING" and db.get(MatchRunRow, done.id).status == "COMPLETED"
        assert latest_run(db) is None or latest_run(db).status in ("COMPLETED", "PARTIAL")
    finally:
        db.rollback()
        db.close()


def test_the_matching_card_ignores_an_abandoned_run(tenant_id):  # noqa: F811
    from app.desktop.views import _match_freshness

    tenant = tenant_id
    db = get_session_factory()()
    try:
        _run(db, "COMPLETED", 400, tenant=tenant)
        _run(db, "RUNNING", 600 * 3, tenant=tenant)  # older than the timeout, but newer rows may exist
        stuck = _run(db, "RUNNING", 90, tenant=tenant)
        freshness = _match_freshness(db, tenant)
        assert freshness["available"] and freshness["running"] is False, "an abandoned RUNNING row is not in progress"
        assert freshness["last_run"].status == "COMPLETED"
        stuck.started_at = db_now() - timedelta(minutes=1)
        db.commit()
        assert _match_freshness(db, tenant)["running"] is True, "a recent run is in progress"
    finally:
        db.rollback()
        db.close()
