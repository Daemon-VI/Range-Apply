"""Failure injection: for each failure the system's answer is retry, handoff,
NEEDS_REVIEW, UNKNOWN or permanent failure — and never a blind resubmit
after submit was invoked. Failures already covered elsewhere are listed at
the bottom rather than duplicated."""

import pytest
from sqlalchemy.exc import OperationalError

from app.application.models import ApplicationStatus
from app.execution.executors import mock as m
from app.execution.models import ExecutorKind, VerificationStatus
from app.pipeline.models import QueueState


def test_prepare_crash_is_retryable_and_execute_crash_after_submit_is_unknown(db_session, tenant_id, scheduler, opportunities, answered_bank):
    from tests.execution.conftest import scripted

    h = scripted(db_session, tenant_id, scheduler, opportunities, default=m.SUCCESS)
    a = h.ready(company="Prep Crash Co")
    b = h.ready(company="After Crash Co")
    h.mock.script[b.id] = m.CRASH_AFTER_SUBMIT
    original = h.mock.prepare

    def crashing_prepare(package):
        if package.application_id == a.id:
            raise RuntimeError("discovery blew up")
        return original(package)

    h.mock.prepare = crashing_prepare
    counts = h.service.run_queue("w1", limit=10, executor_kind=ExecutorKind.MOCK)
    h.refresh(a)
    h.refresh(b)
    assert counts.get("retryable_failure") == 1 and a.status == ApplicationStatus.READY.value and h.item(a).state == QueueState.RETRY_WAIT.value
    assert counts.get("unknown") == 1 and b.status == ApplicationStatus.UNCERTAIN.value and h.mock.submit_calls[b.id] == 1


def test_verification_crash_never_fakes_verified_and_never_fails_the_submit(harness, db_session, monkeypatch):
    attempt = harness.ready(company="Verify Crash Co")

    def boom(package, result):
        raise RuntimeError("history page unreachable")

    monkeypatch.setattr(harness.mock, "verify", boom)
    outcome = harness.execute(attempt)
    run = harness.service.require_run(attempt.last_execution_id)
    assert outcome["outcome"] == "SUBMITTED" and attempt.status == ApplicationStatus.SUBMITTED.value
    assert run.verification_status == VerificationStatus.UNKNOWN.value and "verify crashed" in (run.verification_detail or "")
    assert harness.mock.submit_calls[attempt.id] == 1


def test_missing_or_corrupt_artifact_is_replaced_before_upload_never_uploaded_stale(harness, db_session, tmp_path, monkeypatch):
    from app.config import settings
    from app.documents.database.models import DocumentArtifactRow

    monkeypatch.setattr(settings, "documents_root", str(tmp_path / "docs"))
    attempt = harness.ready(company="Artifact Co")
    first = harness.service.documents.ensure_for_execution(harness.service._context(attempt)[0])
    db_session.commit()
    resume = next(a for a in first.artifacts if a.artifact_type == "RESUME")
    path = tmp_path / "docs" / resume.relative_path
    path.write_bytes(b"%PDF-1.4 tampered by something")
    outcome = harness.execute(attempt)
    assert outcome["outcome"] == "SUBMITTED"
    run = harness.service.require_run(attempt.last_execution_id)
    uploaded = run.diagnostics["artifacts"]["RESUME"]
    stored = db_session.get(DocumentArtifactRow, uploaded["id"])
    assert stored.id != resume.id and stored.status == "ACTIVE" and uploaded["sha256"] == stored.content_hash, "a fresh, verified artifact was used"
    assert db_session.get(DocumentArtifactRow, resume.id).status != "ACTIVE", "the tampered artifact was invalidated, never uploaded"


def test_database_error_during_signal_emission_never_breaks_execution(harness, db_session, monkeypatch):
    import app.signals.service as sig

    def broken_ingest(self, request, process=None):
        raise OperationalError("insert", {}, Exception("disk I/O error"))

    monkeypatch.setattr(sig.SignalIngestionService, "ingest", broken_ingest)
    attempt = harness.ready(company="Signal DB Co")
    outcome = harness.execute(attempt)
    assert outcome["outcome"] == "SUBMITTED" and attempt.status == ApplicationStatus.VERIFIED.value
    assert harness.service.require_run(attempt.last_execution_id).status == "VERIFIED"


def test_audit_write_failure_is_not_swallowed_where_it_matters(harness, db_session, monkeypatch):
    """A refused audit write rolls the execution step back rather than leaving a
    state change without its ledger entry (fail closed, not silently)."""
    from app.pipeline.repository import OpportunityRepository

    attempt = harness.ready(company="Audit Co")
    original = OpportunityRepository.record

    def failing_record(self, *args, **kwargs):
        if args[2] == "execution:submitted":
            raise OperationalError("insert", {}, Exception("audit table locked"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(OpportunityRepository, "record", failing_record)
    with pytest.raises(OperationalError):
        harness.execute(attempt)
    db_session.rollback()
    db_session.refresh(attempt)
    assert attempt.status in (ApplicationStatus.SUBMITTING.value, ApplicationStatus.READY.value)
    monkeypatch.setattr(OpportunityRepository, "record", original)
    # recovery sees a run that had submit invoked: UNCERTAIN, never a second click
    from tests.hardening.conftest import expire_lease

    item = harness.item(attempt)
    db_session.refresh(item)
    if item.state in (QueueState.CLAIMED.value, QueueState.PROCESSING.value):
        expire_lease(db_session, item)
    counts = harness.service.recover_lost_runs()
    db_session.refresh(attempt)
    assert counts.get("uncertain", 0) + counts.get("retryable", 0) >= 1 and attempt.status in (ApplicationStatus.UNCERTAIN.value, ApplicationStatus.READY.value)
    assert harness.mock.submit_calls[attempt.id] == 1


def test_policy_first_touch_race_is_handled(db_session, other_tenant_id):
    """Two workers creating a tenant's policy row at once: one insert wins, the other re-reads."""
    import threading

    from app.database import get_session_factory
    from app.pipeline.repository import PolicyRepository

    factory = get_session_factory()
    errors: list[Exception] = []

    def worker():
        session = factory()
        try:
            for _ in range(10):
                try:
                    PolicyRepository(session, other_tenant_id).get()
                    session.commit()
                    return
                except OperationalError:
                    session.rollback()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    from app.pipeline.database.models import ApplicationPolicyRow

    assert db_session.query(ApplicationPolicyRow).filter_by(tenant_id=other_tenant_id).count() == 1


# Covered elsewhere (not duplicated here):
#   provider timeout / crash / malformed output / budget      tests/ai/test_gateway.py, tests/signals/test_classification.py
#   HTTP timeout / 500 / rate limit / circuit breaker        tests/test_rate_limit_retry.py, tests/discovery/test_resume_and_health.py
#   malformed employer page / unsupported widgets            tests/execution/test_playwright_executor.py
#   browser crash / disconnect                               tests/execution/test_execution_flow.py::test_crash_before_submit_is_retryable_and_after_submit_is_unknown
#   extension disconnect / lease expiry / late result        tests/execution/test_identity_tenancy_concurrency.py, tests/hardening/test_execution_safety.py
#   stale preparation                                        tests/hardening/test_execution_safety.py::test_stale_preparation_never_submits
#   worker process termination                               tests/hardening/test_recovery.py
