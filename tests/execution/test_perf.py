"""Execution state machine at volume: 1,000 READY attempts through the mock executor.

The population mixes successful, retryable, permanent, CAPTCHA, auth, stale,
blocked, cooling and already-submitted attempts. Measures wall time, DB
writes, queue operations, peak traced memory and AI calls (always zero:
execution consumes prepared packages, it never regenerates content).
"""

import os
import time
import tracemalloc
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import event

from app.application.database.models import ApplicationRow
from app.career.database.models import TenantRow
from app.career.importer import SeedImporter
from app.career.models import AnswerBankEntryCreate, AnswerStatus
from app.career.repository import EvidenceRepository
from app.config import settings
from app.core.timeutils import db_now, utc_now
from app.database import get_engine, get_session_factory
from app.execution.database.models import ExecutionRunRow
from app.execution.executors import mock as m
from app.execution.executors.mock import MockExecutor
from app.execution.models import ExecutorKind
from app.execution.service import ExecutionService
from app.intelligence.database.models import MatchRunRow
from app.intelligence.services.match_persistence import ensure_policy
from app.jobs.database.models import JobRow
from app.pipeline.database.models import (
    ApplicationQueueRow,
    CandidateOpportunityRow,
    OpportunityJobRow,
    OpportunityRow,
)
from app.pipeline.models import ApplicationPolicyUpdate
from app.pipeline.policy import company_key
from app.pipeline.repository import PolicyRepository
from app.preparation.database.models import (
    ApplicationPreparationRow,
    PreparationAnswerRow,
    PreparationArtifactRow,
)
from app.preparation.service import PreparationService
from app.scheduler.caps import period_keys
from tests.discovery.conftest import WriteCounter
from tests.preparation.conftest import FACT_ANSWERS

KINDS = [m.SUCCESS] * 10 + [m.SUCCESS_LIKELY, m.RETRYABLE, m.PERMANENT, m.CAPTCHA, m.AUTH, m.UNKNOWN, "STALE", "BLOCKED", "COOLDOWN", "SUBMITTED"]


def _seed(session, tenant_id: str, n: int, url_for=None, plain: bool = False) -> tuple[dict[str, str], dict]:
    """Seed ``n`` READY attempts with real preparations.

    ``url_for(i, kind) -> (application_url, source)`` lets the browser
    benchmark point every attempt at a local fixture; ``plain`` seeds only
    ordinary READY attempts (no blocked / cooling / stale / submitted rows),
    leaving the outcome to the page itself.
    """
    ensure_policy(session)
    run = MatchRunRow(policy_version="v1", engine_version="1.1.0", career_brain_version="v1", started_at=db_now(), completed_at=db_now(), status="COMPLETED", trigger="perf6", errors=[], tenant_id=tenant_id)
    session.add(run)
    session.flush()
    suffix = uuid.uuid4().hex[:6]
    keys = period_keys(utc_now(), "UTC")
    now = db_now()
    blocked_company = f"Blocked6 {suffix}"
    cooling_company = f"Cooling6 {suffix}"
    jobs, opps, cos, preps, artifacts, answers, attempts, items = [], [], [], [], [], [], [], []
    script: dict[str, str] = {}
    expected = {k: 0 for k in set(KINDS)}
    for i in range(n):
        kind = m.SUCCESS if plain else KINDS[i % len(KINDS)]
        expected[kind] += 1
        company = blocked_company if kind == "BLOCKED" else cooling_company if kind == "COOLDOWN" else f"Perf6 {suffix} {i}"
        # Cooling openings get distinct titles so the cool-down, not the
        # same-title duplicate rule, is what blocks them.
        title = f"Cooling Role {i}" if kind == "COOLDOWN" else f"Engineer {i % 7}"
        application_url, source = (url_for(i, kind) if url_for else (f"https://perf6.example/{suffix}/{i}/apply", "GREENHOUSE"))
        job = JobRow(id=str(uuid.uuid4()), canonical_key=f"perf6-{suffix}-{i}", source=source, source_job_id=f"perf6-{suffix}-{i}", company=company, title=title, original_title=title, description="Python backend role.", original_description="Python backend role.", location="Remote", remote_type="REMOTE", source_url=f"https://perf6.example/{suffix}/{i}", application_url=application_url, content_hash=f"hash-{i}", processing_status="NORMALIZED", job_status="ACTIVE", first_seen_at=now, last_seen_at=now)
        opp = OpportunityRow(id=str(uuid.uuid4()), identity_key=f"perf6-{suffix}-{i}", canonical_job_id=job.id, company=company, company_key=company_key(company), title=job.title, location_bucket="remote", status="OPEN", first_seen_at=now, last_seen_at=now)
        co = CandidateOpportunityRow(id=str(uuid.uuid4()), tenant_id=tenant_id, opportunity_id=opp.id, state="PREPARED", eligibility_status="ELIGIBLE", fit_score=80, fit_band="HIGH", priority_score=(i * 37) % 100, policy_admitted=True, policy_reason="perf")
        prep = ApplicationPreparationRow(id=str(uuid.uuid4()), tenant_id=tenant_id, candidate_opportunity_id=co.id, opportunity_id=opp.id, job_id=job.id, job_content_hash=job.content_hash, version=1, tailoring_level="L0", lane="AUTO", cover_letter_mode="DISABLED", status="READY", validation_status="PASSED", validation_report={}, input_fingerprint=f"fp-{i}" if kind != "STALE" else "stale", inputs={"job_id": job.id, "job_content_hash": job.content_hash if kind != "STALE" else "old", "questions": []}, evidence_keys=["skill-python"], ai_used=False, ai_calls=0)
        artifacts.append(PreparationArtifactRow(tenant_id=tenant_id, preparation_id=prep.id, artifact_type="RESUME", content="SKILLS\n- Skills: Python, Go", blocks=[{"kind": "CLAIM", "section": "skills", "text": "Skills: Python, Go", "evidence_keys": ["skill-python"]}, {"kind": "CLAIM", "section": "projects", "text": f"Project {i}: backend service. Technologies: Python.", "evidence_keys": ["ticket-engine"]}], evidence_keys=["skill-python"], template_version="prep-v1", validation_status="PASSED"))
        answers.append(PreparationAnswerRow(tenant_id=tenant_id, preparation_id=prep.id, position=0, question="Why are you interested in this role?", question_key="why are you interested in this role", category="why_role", answer="Because it fits.", evidence_keys=["skill-python"], source="GENERATED", status="ANSWERED", required=True))
        status = "SUBMITTED" if kind == "SUBMITTED" else "READY"
        attempt = ApplicationRow(id=str(uuid.uuid4()), job_id=job.id, tenant_id=tenant_id, opportunity_id=opp.id, candidate_opportunity_id=co.id, preparation_id=prep.id, status=status, attempt_number=1, lane="AUTO", tailoring_level="L0", cap_day=keys.day.key, cap_week=keys.week.key, reserved_at=now, submitted_at=now - timedelta(days=1) if kind == "SUBMITTED" else None)
        co.application_id = attempt.id
        items.append(ApplicationQueueRow(id=str(uuid.uuid4()), tenant_id=tenant_id, candidate_opportunity_id=co.id, opportunity_id=opp.id, action="SUBMIT", state="PENDING", lane="AUTO", priority=co.priority_score, idempotency_key=f"{tenant_id}:{opp.id}:SUBMIT", available_at=now))
        if kind in (m.SUCCESS, m.SUCCESS_LIKELY, m.RETRYABLE, m.PERMANENT, m.CAPTCHA, m.AUTH, m.UNKNOWN):
            script[attempt.id] = kind
        jobs.append(job)
        opps.append(opp)
        cos.append(co)
        preps.append(prep)
        attempts.append(attempt)
    if plain:
        for batch in (jobs, opps, cos, preps, artifacts, answers, attempts, items):
            session.add_all(batch)
            session.flush()
        session.add_all([OpportunityJobRow(opportunity_id=o.id, job_id=o.canonical_job_id, linked_by="new") for o in opps])
        session.commit()
        PolicyRepository(session, tenant_id).update(ApplicationPolicyUpdate(daily_cap=100000, weekly_cap=100000, cooldown_days=0), "perf")
        session.commit()
        prep_service = PreparationService(session, tenant_id, actor="perf")
        for prep in preps:
            prep.input_fingerprint, prep.inputs = prep_service.current_inputs(prep)
        session.commit()
        return script, expected
    # An earlier submission at the cooling company, 2 days ago.
    cool_job = JobRow(id=str(uuid.uuid4()), canonical_key=f"perf6-{suffix}-cool", source="GREENHOUSE", source_job_id="cool", company=cooling_company, title="Staff Engineer", original_title="Staff Engineer", description="x", original_description="x", location="Remote", remote_type="REMOTE", source_url="https://perf6.example/cool", content_hash="c", processing_status="NORMALIZED", job_status="ACTIVE", first_seen_at=now, last_seen_at=now)
    cool_opp = OpportunityRow(id=str(uuid.uuid4()), identity_key=f"perf6-{suffix}-cool", canonical_job_id=cool_job.id, company=cooling_company, company_key=company_key(cooling_company), title="Staff Engineer", location_bucket="remote", status="OPEN", first_seen_at=now, last_seen_at=now)
    cool_co = CandidateOpportunityRow(id=str(uuid.uuid4()), tenant_id=tenant_id, opportunity_id=cool_opp.id, state="SUBMITTED", eligibility_status="ELIGIBLE", fit_score=80, fit_band="HIGH", priority_score=50, policy_admitted=True)
    attempts.append(ApplicationRow(id=str(uuid.uuid4()), job_id=cool_job.id, tenant_id=tenant_id, opportunity_id=cool_opp.id, candidate_opportunity_id=cool_co.id, status="SUBMITTED", attempt_number=1, cap_day="2000-01-01", cap_week="2000-W01", reserved_at=now - timedelta(days=2), submitted_at=now - timedelta(days=2)))
    jobs.append(cool_job)
    opps.append(cool_opp)
    cos.append(cool_co)
    for batch in (jobs, opps, cos, preps, artifacts, answers, attempts, items):
        session.add_all(batch)
        session.flush()
    session.add_all([OpportunityJobRow(opportunity_id=o.id, job_id=o.canonical_job_id, linked_by="new") for o in opps])
    session.commit()
    PolicyRepository(session, tenant_id).update(ApplicationPolicyUpdate(daily_cap=100000, weekly_cap=100000, cooldown_days=30, blocked_companies=[blocked_company]), "perf")
    session.commit()
    # Real fingerprints, so the stale check does real work; STALE rows keep a
    # job hash the employer has since changed.
    prep_service = PreparationService(session, tenant_id, actor="perf")
    stale_ids = {p.id for p, k in zip(preps, KINDS * (n // len(KINDS) + 1)) if k == "STALE"}
    for prep in preps:
        digest, inputs = prep_service.current_inputs(prep)
        if prep.id in stale_ids:
            inputs = {**inputs, "job_content_hash": "old"}
            digest = "stale-" + digest[:50]
        prep.input_fingerprint, prep.inputs = digest, inputs
    session.commit()
    return script, expected


def _cleanup(session, tenant_id: str) -> None:
    session.rollback()
    row = session.get(TenantRow, tenant_id)
    if row is not None:
        session.delete(row)
        session.commit()
    for opp in session.query(OpportunityRow).filter(OpportunityRow.identity_key.like("perf6-%")).all():
        session.delete(opp)
    for job in session.query(JobRow).filter(JobRow.canonical_key.like("perf6-%")).all():
        session.delete(job)
    for run in session.query(MatchRunRow).filter(MatchRunRow.trigger == "perf6").all():
        session.delete(run)
    session.commit()


def _benchmark(n: int) -> dict:
    session = get_session_factory()()
    tenant_id = f"perf-p6-{uuid.uuid4().hex[:6]}"
    counter = WriteCounter()
    engine = get_engine()
    try:
        session.add(TenantRow(id=tenant_id, name="perf6"))
        session.commit()
        repo = EvidenceRepository(session, tenant_id)
        SeedImporter(repo).import_file(settings.career_data_path)
        repo.upsert_profile({"email": "perf@example.com"}, None, "perf")
        for category, question, answer in FACT_ANSWERS:
            repo.create_answer(AnswerBankEntryCreate(category=category, question=question, answer=answer, status=AnswerStatus.APPROVED), "perf")
        repo.commit()
        script, expected = _seed(session, tenant_id, n)
        mock = MockExecutor(script=script, default=m.SUCCESS)
        service = ExecutionService(session, tenant_id, actor="perf", executors={ExecutorKind.MOCK: mock})
        event.listen(engine, "after_cursor_execute", counter)
        tracemalloc.start()
        started = time.perf_counter()
        totals: dict[str, int] = {}
        for _ in range(n // 100 + 2):  # retryable items come back after their backoff; bound the loop
            counts = service.run_queue("perf-worker", limit=100, executor_kind=ExecutorKind.MOCK)
            if not counts.get("claimed"):
                break
            for k, v in counts.items():
                totals[k] = totals.get(k, 0) + v
        seconds = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        event.remove(engine, "after_cursor_execute", counter)

        by_status = service.summary().attempts_by_status
        runs = dict(session.query(ExecutionRunRow.status, __import__("sqlalchemy").func.count(ExecutionRunRow.id)).filter_by(tenant_id=tenant_id).group_by(ExecutionRunRow.status).all())
        assert totals["claimed"] >= n  # RETRY_WAIT items may be claimed again once their backoff lapses
        assert sum(mock.submit_calls.values()) == expected[m.SUCCESS] + expected[m.SUCCESS_LIKELY] + expected[m.UNKNOWN]
        assert all(v == 1 for v in mock.submit_calls.values()), "no attempt pressed submit twice"
        assert by_status.get("VERIFIED") == expected[m.SUCCESS]
        assert by_status.get("SUBMITTED") == expected[m.SUCCESS_LIKELY] + expected["SUBMITTED"] + 1
        assert by_status.get("UNCERTAIN") == expected[m.UNKNOWN]
        assert by_status.get("BLOCKED") == expected[m.CAPTCHA] + expected[m.AUTH] + expected["COOLDOWN"]
        assert by_status.get("FAILED") == expected[m.PERMANENT]
        assert by_status.get("CLOSED") == expected["BLOCKED"]
        assert by_status.get("PREPARING") == expected["STALE"]
        # Retryable items stay READY, or reach NEEDS_REVIEW once their retries are
        # exhausted when the loop outlives the 60 s backoff on a slow machine.
        assert by_status.get("READY", 0) + by_status.get("NEEDS_REVIEW", 0) == expected[m.RETRYABLE]
        assert runs.get("STALE") == expected["STALE"]
        results = {
            "attempts": n,
            "seconds": round(seconds, 2),
            "per_minute": round(n / max(seconds, 1e-6) * 60),
            "claimed": totals["claimed"],
            "outcomes": {k: v for k, v in totals.items() if k != "claimed"},
            "attempts_by_status": by_status,
            "runs_by_status": runs,
            "submit_calls": sum(mock.submit_calls.values()),
            "queue_items": session.query(ApplicationQueueRow).filter_by(tenant_id=tenant_id).count(),
            "db_writes": counter.writes,
            "db_selects": counter.selects,
            "peak_memory_mb": round(peak / 1024 / 1024, 1),
            "ai_calls": 0,
        }
        print("\nEXECUTION BENCHMARK:", results)
        return results
    finally:
        _cleanup(session, tenant_id)
        session.close()


@pytest.mark.perf
def test_execute_1000_mixed_population():
    results = _benchmark(1000)
    # The bound is a sanity guard for a laptop (Phase 8 renders a PDF per attempt), not a target.
    assert results["ai_calls"] == 0 and results["seconds"] < 600


@pytest.mark.perf
@pytest.mark.skipif(os.environ.get("CAREEROS_PERF") != "1", reason="set CAREEROS_PERF=1 for the 5,000-attempt run")
def test_execute_5000_mixed_population():
    results = _benchmark(5000)
    assert results["ai_calls"] == 0
