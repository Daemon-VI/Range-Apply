"""Fixtures for the Phase 12 hardening group: the Phase 6 execution harness
(real scheduler, zero-AI preparation, mock executor), a second tenant, an
API client bound to the tenant, and small helpers to fake crashes."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.core.timeutils import db_now
from app.main import app
from tests.execution import conftest as _exec

OpportunityFactory = _exec.OpportunityFactory
db_session = _exec.db_session
tenant_id = _exec.tenant_id
other_tenant_id = _exec.other_tenant_id
evidence = _exec.evidence
answered_bank = _exec.answered_bank
jobs = _exec.jobs
matches = _exec.matches
opportunities = _exec.opportunities
scheduler = _exec.scheduler
other_scheduler = _exec.other_scheduler
harness = _exec.harness
scripted = _exec.scripted


_live_submission_enabled = _exec._live_submission_enabled


@pytest.fixture(autouse=True)
def _no_ai():
    from app.ai.gateway import reset_gateway

    reset_gateway(None)
    yield
    reset_gateway(None)


@pytest.fixture
def client(tenant_id, db_session):
    previous = app.dependency_overrides.get(get_tenant_id)
    app.dependency_overrides[get_tenant_id] = lambda: tenant_id
    db_session.commit()
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        if previous is not None:
            app.dependency_overrides[get_tenant_id] = previous
        else:
            app.dependency_overrides.pop(get_tenant_id, None)


def as_tenant(tid: str) -> None:
    app.dependency_overrides[get_tenant_id] = lambda: tid


def expire_lease(session, item, seconds: int = 60) -> None:
    """Pretend the worker holding ``item`` went silent: its lease is in the past."""
    item.lease_expires_at = db_now() - timedelta(seconds=seconds)
    session.commit()


def begin(harness, attempt, worker: str = "w1", executor=None):
    """Claim + start one attempt without executing (a worker that then dies)."""
    from app.execution.executors.extension import BrowserExtensionExecutor
    from app.execution.models import ExecutorKind

    harness.service.executors.setdefault(ExecutorKind.BROWSER_EXTENSION, BrowserExtensionExecutor())
    item = harness.item(attempt)
    claimed = harness.service.claim_item(item, worker)
    assert claimed is not None, "item not claimable"
    run, package, outcome = harness.service.start(claimed, worker, executor or ExecutorKind.MOCK)
    assert outcome["outcome"] == "started", outcome
    return claimed, run, package


__all__ = ["as_tenant", "begin", "expire_lease"]
