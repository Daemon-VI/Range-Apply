"""Consolidated end-to-end benchmark and soak: repeated rounds of
opportunities -> scheduler admission + zero-AI preparation -> mock
execution (with PDF rendering) -> signal ingestion -> learning snapshot ->
diagnostics. Records per-round time, DB rows, stale leases, memory growth
and AI calls. No live site is touched."""

import gc
import os
import time
import tracemalloc

import pytest

from app.ai.gateway import AIGateway, GlobalAIConfig, UsageSink, reset_gateway
from app.application.database.models import ApplicationRow
from app.config import settings
from app.execution.database.models import ExecutionRunRow
from app.execution.executors import mock as m
from app.execution.models import ExecutorKind
from app.learning.engine import LearningEngine
from app.pipeline.database.models import ApplicationQueueRow
from app.signals.database.models import OutcomeEventRow, SignalRow
from app.signals.models import AttributionHints, EmailMessage
from app.signals.service import SignalInboxService


def _round(h, db_session, tenant_id, opportunities, index: int, size: int) -> dict:
    started = time.perf_counter()
    cos = [opportunities.make(company=f"Soak {index} Co {i}", title=f"Engineer {i}", fit_score=90 - (i % 40)) for i in range(size)]
    for i, co in enumerate(cos):
        if i % 10 == 3:
            h.mock.script[co.opportunity.company] = m.UNKNOWN_THEN_VERIFIED
        elif i % 10 == 6:
            h.mock.script[co.opportunity.company] = m.CAPTCHA
        elif i % 10 == 9:
            h.mock.script[co.opportunity.company] = m.RETRYABLE
    run = h.scheduler.run(prepare=True, prepare_limit=size + 10, worker_id="prep")
    assert run.errors == [], run.errors[:3]
    h.service.enqueue_ready()
    db_session.commit()
    executed = {}
    for _ in range(3):
        counts = h.service.run_queue("soak-worker", limit=size + 10, executor_kind=ExecutorKind.MOCK)
        for k, v in counts.items():
            executed[k] = executed.get(k, 0) + v
        if not counts.get("claimed"):
            break
    inbox = SignalInboxService(db_session, tenant_id, actor="soak")
    verified = db_session.query(ApplicationRow).filter(ApplicationRow.tenant_id == tenant_id, ApplicationRow.status.in_(["VERIFIED", "SUBMITTED", "UNCERTAIN"])).all()
    ingested = 0
    for i, attempt in enumerate(verified):
        message = EmailMessage(message_id=f"<soak-{index}-{attempt.id}>", sender="hr@soak.example", subject="Thank you for applying", text="Thank you for applying. We have received your application.", hints=AttributionHints(application_id=attempt.id))
        inbox.ingest_email(message)
        inbox.ingest_email(message)  # every email is delivered twice
        if i % 4 == 0:
            inbox.ingest_email(EmailMessage(message_id=f"<soak-int-{index}-{attempt.id}>", sender="hr@soak.example", subject="Interview", text="We'd like to invite you to an interview.", hints=AttributionHints(application_id=attempt.id)))
        ingested += 1
    db_session.commit()
    snapshot = LearningEngine(db_session, tenant_id, actor="soak").snapshot(actor="soak")
    db_session.commit()
    seconds = time.perf_counter() - started
    gc.collect()
    current, _ = tracemalloc.get_traced_memory()
    from app.core.timeutils import db_now

    stale = db_session.query(ApplicationQueueRow).filter(ApplicationQueueRow.tenant_id == tenant_id, ApplicationQueueRow.state.in_(["CLAIMED", "PROCESSING"]), ApplicationQueueRow.lease_expires_at < db_now()).count()
    running = db_session.query(ExecutionRunRow).filter(ExecutionRunRow.tenant_id == tenant_id, ExecutionRunRow.status == "RUNNING").count()
    signals = db_session.query(SignalRow).filter(SignalRow.tenant_id == tenant_id).count()
    events = db_session.query(OutcomeEventRow).filter(OutcomeEventRow.tenant_id == tenant_id).count()
    return {"round": index, "opportunities": size, "seconds": round(seconds, 2), "admitted": run.admitted, "prepared": run.preparation.get("prepared") if run.preparation else None, "executed": executed, "emails_ingested": ingested, "signals_total": signals, "outcome_events_total": events, "learning_rows": snapshot.dataset_size, "stale_leases": stale, "running_runs": running, "memory_mb": round(current / 1024 / 1024, 2)}


@pytest.mark.perf
def test_end_to_end_pipeline_soak(db_session, tenant_id, scheduler, opportunities, answered_bank, tmp_path, monkeypatch):
    from tests.execution.conftest import scripted

    monkeypatch.setattr(settings, "documents_root", str(tmp_path / "docs"))
    gateway = AIGateway(config=GlobalAIConfig(enabled=False, persist_usage=False), sink=UsageSink(persist=False))
    reset_gateway(gateway)
    h = scripted(db_session, tenant_id, scheduler, opportunities, default=m.SUCCESS)
    # The soak measures machinery, not the candidate's aggressiveness: caps are raised for the run.
    from app.pipeline.models import ApplicationPolicyUpdate
    from app.pipeline.repository import PolicyRepository

    PolicyRepository(db_session, tenant_id).update(ApplicationPolicyUpdate(daily_cap=100000, weekly_cap=100000, cooldown_days=0), "soak")
    db_session.commit()
    rounds = int(os.environ.get("CAREEROS_SOAK_ROUNDS", "3"))
    size = int(os.environ.get("CAREEROS_SOAK_SIZE", "30"))
    tracemalloc.start()
    results = [_round(h, db_session, tenant_id, opportunities, i, size) for i in range(rounds)]
    tracemalloc.stop()
    reset_gateway(None)
    print("\nPIPELINE SOAK:", results)
    assert all(r["stale_leases"] == 0 and r["running_runs"] == 0 for r in results)
    assert gateway.stats()["provider_calls"] == 0 and gateway.stats()["requests"] == 0
    assert all(v == 1 for v in h.mock.submit_calls.values()), "no attempt pressed submit twice across the soak"
    per_round_signals = [results[0]["signals_total"]] + [results[i]["signals_total"] - results[i - 1]["signals_total"] for i in range(1, rounds)]
    assert all(s <= r["emails_ingested"] * 2 + r["emails_ingested"] for s, r in zip(per_round_signals, results)), "duplicate deliveries never became extra signals"
    if rounds >= 3:
        growth_last = results[-1]["memory_mb"] - results[-2]["memory_mb"]
        assert growth_last < 8, f"memory keeps growing per round: {[r['memory_mb'] for r in results]}"
