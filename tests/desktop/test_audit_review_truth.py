"""The Review checklist and answer boxes tell the truth about the latest run (integration fixes, 2026-09-14)."""

from datetime import datetime
from types import SimpleNamespace

from app.desktop.views import preflight_checks


def _run(number, status, handoff=None):
    return SimpleNamespace(run_number=number, status=status, handoff_reason=handoff, error_class=None, diagnostics={"fields_filled": 6}, finished_at=datetime(2026, 9, 14, 8, 56))


def _check(checks, name):
    return next(c for c in checks if c["name"] == name)


def _checks(runs):
    co = SimpleNamespace(eligibility_status="ELIGIBLE")
    prep = SimpleNamespace(status="READY", version=3, validation_status="PASSED")
    attempt = SimpleNamespace(status="BLOCKED")
    return preflight_checks(co, prep, [], [], [], [], [], runs, attempt)


def test_an_older_dry_run_does_not_mark_dry_run_complete_after_a_newer_handoff():
    stale = _check(_checks([_run(3, "HANDOFF", "UNKNOWN_REQUIRED_FIELD"), _run(2, "DRY_RUN")]), "Dry run complete")
    assert stale["state"] == "warn" and "#3" in stale["detail"] and "no longer reflects" in stale["detail"]
    fresh = _check(_checks([_run(4, "DRY_RUN"), _run(3, "HANDOFF", "UNKNOWN_REQUIRED_FIELD")]), "Dry run complete")
    assert fresh["state"] == "pass" and "#4" in fresh["detail"]
