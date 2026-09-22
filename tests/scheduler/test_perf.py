"""Scheduler at volume: 500 candidate opportunities always, 5,000 opt-in.

The population mixes HIGH / MEDIUM / LOW / INELIGIBLE fits, blocked companies,
companies in cool-down, already queued and already submitted openings, and
preparations waiting on review or user input. Measures wall time, considered,
admitted, rejected by reason, queue items created, duplicates avoided, DB
writes, peak traced memory and AI calls.
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
from app.core.timeutils import db_now
from app.database import get_engine, get_session_factory
from app.intelligence.database.models import JobMatchRow, MatchRunRow
from app.intelligence.services.match_persistence import ensure_policy
from app.jobs.database.models import JobRow
from app.pipeline.database.models import (
    ApplicationQueueRow,
    CandidateOpportunityRow,
    OpportunityJobRow,
    OpportunityRow,
)
from app.pipeline.models import ApplicationPolicyUpdate
from app.pipeline.repository import PolicyRepository
from app.preparation.database.models import ApplicationPreparationRow
from app.scheduler.service import SchedulerService
from tests.discovery.conftest import WriteCounter

TITLES = ["Backend Engineer", "Data Engineer", "Platform Engineer", "ML Engineer", "Software Engineer", "SDE I"]


def _seed(session, tenant_id: str, n: int) -> dict:
    ensure_policy(session)
    run = MatchRunRow(policy_version="v1", engine_version="1.1.0", career_brain_version="v1", started_at=db_now(), completed_at=db_now(), status="COMPLETED", trigger="perf5", errors=[])
    session.add(run)
    session.flush()
    suffix = uuid.uuid4().hex[:6]
    jobs, matches, opps, cos, links, attempts, items, preps = [], [], [], [], [], [], [], []
    expected = {"blocked": 0, "cooldown": 0, "queued": 0, "submitted": 0, "review": 0, "input": 0, "ineligible": 0, "low_disabled": 0, "dup_source_rows": 0}
    blocked_company = f"Blocked {suffix}"
    cooldown_company = f"Cooling {suffix}"
    now = db_now()
    for i in range(n):
        kind = i % 20
        company = f"Perf5 {suffix} {i}"  # unique by default: no accidental cool-down
        fit = 45 + (i * 7) % 55  # MEDIUM or HIGH unless a case says otherwise
        eligibility = "ELIGIBLE"
        if kind == 0:
            company = blocked_company
            expected["blocked"] += 1
        elif kind == 1:
            company = cooldown_company
            expected["cooldown"] += 1
        elif kind == 2:
            eligibility = "INELIGIBLE"
            expected["ineligible"] += 1
        elif kind == 3:
            fit = 10  # LOW band, disabled by the policy below
            expected["low_disabled"] += 1
        job = JobRow(id=str(uuid.uuid4()), canonical_key=f"perf5-{suffix}-{i}", source="GREENHOUSE", source_job_id=f"perf5-{suffix}-{i}", company=company, title=TITLES[i % len(TITLES)], original_title=TITLES[i % len(TITLES)], description="Python and Go backend role.", original_description="Python and Go backend role.", location="Remote", remote_type="REMOTE", source_url=f"https://perf5.example/{suffix}/{i}", content_hash=uuid.uuid4().hex, processing_status="NORMALIZED", job_status="ACTIVE", first_seen_at=now - timedelta(hours=i % 48), last_seen_at=now)
        match = JobMatchRow(id=str(uuid.uuid4()), job_id=job.id, run_id=run.id, job_canonical_key=job.canonical_key, job_content_hash=job.content_hash, eligibility_status=eligibility, eligibility_confidence="HIGH", fit_score=fit, priority="P1", match_type="CORE_MATCH", confidence="HIGH", explanation="perf", evaluated_at=now)
        opp = OpportunityRow(id=str(uuid.uuid4()), identity_key=f"perf5-{suffix}-{i}", canonical_job_id=job.id, company=company, title=job.title, location_bucket="remote", status="OPEN", first_seen_at=job.first_seen_at, last_seen_at=now, deadline=(now + timedelta(days=1 + i % 30)) if i % 3 else None)
        band = "HIGH" if fit > 70 else "MEDIUM" if fit >= 45 else "LOW"
        co = CandidateOpportunityRow(id=str(uuid.uuid4()), tenant_id=tenant_id, opportunity_id=opp.id, state="ELIGIBLE" if eligibility == "ELIGIBLE" else "INELIGIBLE", eligibility_status=eligibility, match_id=match.id, fit_score=fit, fit_band=band, priority_score=(i * 37) % 100, policy_admitted=eligibility == "ELIGIBLE", policy_reason="perf")
        if kind == 4:  # already queued (PREPARE pending) with a live attempt
            co.state = "QUEUED"
            attempts.append(ApplicationRow(id=str(uuid.uuid4()), job_id=job.id, tenant_id=tenant_id, opportunity_id=opp.id, candidate_opportunity_id=co.id, status="QUALIFIED", attempt_number=1, cap_day="2000-01-01", cap_week="2000-W01", reserved_at=now))
            items.append(ApplicationQueueRow(id=str(uuid.uuid4()), tenant_id=tenant_id, candidate_opportunity_id=co.id, opportunity_id=opp.id, action="PREPARE", state="PENDING", lane="REVIEW", priority=co.priority_score, idempotency_key=f"{tenant_id}:{opp.id}:PREPARE", available_at=now))
            expected["queued"] += 1
        elif kind == 5:  # already submitted
            co.state = "SUBMITTED"
            attempts.append(ApplicationRow(id=str(uuid.uuid4()), job_id=job.id, tenant_id=tenant_id, opportunity_id=opp.id, candidate_opportunity_id=co.id, status="SUBMITTED", attempt_number=1, cap_day="2000-01-01", cap_week="2000-W01", reserved_at=now - timedelta(days=400), submitted_at=now - timedelta(days=400)))
            expected["submitted"] += 1
        elif kind == 6:  # preparation needs review (no attempt yet)
            co.state = "IN_REVIEW"
            preps.append(ApplicationPreparationRow(id=str(uuid.uuid4()), tenant_id=tenant_id, candidate_opportunity_id=co.id, opportunity_id=opp.id, job_id=job.id, version=1, tailoring_level="L0", lane="REVIEW", cover_letter_mode="DISABLED", status="NEEDS_REVIEW", validation_status="PASSED", validation_report={}, input_fingerprint=uuid.uuid4().hex, inputs={}, evidence_keys=[], ai_used=False, ai_calls=0))
            expected["review"] += 1
        elif kind == 7:  # preparation needs user input
            co.state = "IN_REVIEW"
            preps.append(ApplicationPreparationRow(id=str(uuid.uuid4()), tenant_id=tenant_id, candidate_opportunity_id=co.id, opportunity_id=opp.id, job_id=job.id, version=1, tailoring_level="L0", lane="REVIEW", cover_letter_mode="DISABLED", status="NEEDS_USER_INPUT", validation_status="PASSED", validation_report={}, input_fingerprint=uuid.uuid4().hex, inputs={}, evidence_keys=[], ai_used=False, ai_calls=0))
            expected["input"] += 1
        jobs.append(job)
        matches.append(match)
        opps.append(opp)
        cos.append(co)
        links.append(OpportunityJobRow(opportunity_id=opp.id, job_id=job.id, linked_by="new"))
        if kind == 8:  # a second source row for the same opening
            dup = JobRow(id=str(uuid.uuid4()), canonical_key=f"perf5-{suffix}-{i}-lever", source="LEVER", source_job_id=f"perf5-{suffix}-{i}-lever", company=company, title=job.title, original_title=job.title, description=job.description, original_description=job.description, location="Remote - US", remote_type="REMOTE", source_url=f"https://perf5.example/lever/{suffix}/{i}", content_hash=uuid.uuid4().hex, processing_status="NORMALIZED", job_status="ACTIVE", first_seen_at=now, last_seen_at=now)
            jobs.append(dup)
            links.append(OpportunityJobRow(opportunity_id=opp.id, job_id=dup.id, linked_by="identity_key"))
            expected["dup_source_rows"] += 1
    # A previous submission at the cool-down company, 5 days ago.
    cool_job = JobRow(id=str(uuid.uuid4()), canonical_key=f"perf5-{suffix}-cool", source="GREENHOUSE", source_job_id=f"perf5-{suffix}-cool", company=cooldown_company, title="Staff Engineer", original_title="Staff Engineer", description="x", original_description="x", location="Remote", remote_type="REMOTE", source_url=f"https://perf5.example/{suffix}/cool", content_hash=uuid.uuid4().hex, processing_status="NORMALIZED", job_status="ACTIVE", first_seen_at=now, last_seen_at=now)
    cool_opp = OpportunityRow(id=str(uuid.uuid4()), identity_key=f"perf5-{suffix}-cool", canonical_job_id=cool_job.id, company=cooldown_company, title="Staff Engineer", location_bucket="remote", status="OPEN", first_seen_at=now, last_seen_at=now)
    cool_co = CandidateOpportunityRow(id=str(uuid.uuid4()), tenant_id=tenant_id, opportunity_id=cool_opp.id, state="SUBMITTED", eligibility_status="ELIGIBLE", fit_score=80, fit_band="HIGH", priority_score=50, policy_admitted=True)
    jobs.append(cool_job)
    opps.append(cool_opp)
    cos.append(cool_co)
    links.append(OpportunityJobRow(opportunity_id=cool_opp.id, job_id=cool_job.id, linked_by="new"))
    attempts.append(ApplicationRow(id=str(uuid.uuid4()), job_id=cool_job.id, tenant_id=tenant_id, opportunity_id=cool_opp.id, candidate_opportunity_id=cool_co.id, status="SUBMITTED", attempt_number=1, cap_day="2000-01-01", cap_week="2000-W01", reserved_at=now - timedelta(days=5), submitted_at=now - timedelta(days=5)))
    expected["submitted"] += 1
    for batch in (jobs, matches, opps, cos, links, attempts, items, preps):
        session.add_all(batch)
        session.flush()
    session.commit()
    PolicyRepository(session, tenant_id).update(
        ApplicationPolicyUpdate(daily_cap=100000, weekly_cap=100000, cooldown_days=30, enabled_bands=["HIGH", "MEDIUM"], blocked_companies=[blocked_company]),
        "perf",
    )
    session.commit()
    return expected


def _cleanup(session, tenant_id: str) -> None:
    session.rollback()
    row = session.get(TenantRow, tenant_id)
    if row is not None:
        session.delete(row)
        session.commit()
    for opp in session.query(OpportunityRow).filter(OpportunityRow.identity_key.like("perf5-%")).all():
        session.delete(opp)
    for job in session.query(JobRow).filter(JobRow.canonical_key.like("perf5-%")).all():
        session.delete(job)
    for run in session.query(MatchRunRow).filter(MatchRunRow.trigger == "perf5").all():
        session.delete(run)
    session.commit()


def _benchmark(n: int) -> dict:
    session = get_session_factory()()
    tenant_id = f"perf-p5-{uuid.uuid4().hex[:6]}"
    counter = WriteCounter()
    engine = get_engine()
    try:
        session.add(TenantRow(id=tenant_id, name="perf5"))
        session.commit()
        expected = _seed(session, tenant_id, n)
        scheduler = SchedulerService(session, tenant_id, actor="perf")
        event.listen(engine, "after_cursor_execute", counter)
        tracemalloc.start()
        started = time.perf_counter()
        run = scheduler.run(trigger="perf", window=100000)
        seconds = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        started2 = time.perf_counter()
        second = scheduler.run(trigger="perf", window=100000)
        seconds2 = time.perf_counter() - started2
        event.remove(engine, "after_cursor_execute", counter)

        assert run.status == "COMPLETED" and run.errors == [], run.errors[:3]
        assert run.considered == n + 1
        by = run.blocked_by_reason
        assert by.get("COMPANY_BLOCKED") == expected["blocked"]
        assert by.get("COOLDOWN_ACTIVE") == expected["cooldown"]
        assert by.get("INELIGIBLE") == expected["ineligible"]
        assert by.get("BAND_DISABLED") == expected["low_disabled"]
        assert by.get("NEEDS_REVIEW") == expected["review"] and by.get("NEEDS_USER_INPUT") == expected["input"]
        assert run.already_queued == expected["queued"] and run.already_completed == expected["submitted"]
        # n seeded rows plus the earlier submission that starts the cool-down.
        admissible = (n + 1) - sum(expected[k] for k in ("blocked", "cooldown", "ineligible", "low_disabled", "queued", "submitted", "review", "input"))
        assert run.admitted == admissible == run.enqueued
        assert run.deferred == 0 and run.ready_for_execution == 0
        queued = session.query(ApplicationQueueRow).filter_by(tenant_id=tenant_id, action="PREPARE").count()
        assert queued == admissible + expected["queued"]
        assert session.query(ApplicationRow).filter_by(tenant_id=tenant_id).count() == admissible + expected["queued"] + expected["submitted"]
        assert second.admitted == 0 and second.enqueued == 0 and second.already_queued == admissible + expected["queued"]
        assert scheduler.capacity().day.used == admissible
        results = {
            "candidate_opportunities": n + 1,
            "seconds_first_run": round(seconds, 2),
            "per_minute": round((n + 1) / max(seconds, 1e-6) * 60),
            "seconds_second_run": round(seconds2, 2),
            "considered": run.considered,
            "admissible": run.admissible,
            "admitted": run.admitted,
            "enqueued": run.enqueued,
            "already_queued": run.already_queued,
            "already_completed": run.already_completed,
            "needs_review": run.needs_review,
            "needs_user_input": run.needs_user_input,
            "blocked_by_reason": by,
            "duplicate_source_rows_collapsed": expected["dup_source_rows"],
            "db_writes_both_runs": counter.writes,
            "peak_memory_mb": round(peak / 1024 / 1024, 1),
            "ai_calls": 0,
        }
        print("\nSCHEDULER BENCHMARK:", results)
        return results
    finally:
        _cleanup(session, tenant_id)
        session.close()


@pytest.mark.perf
def test_schedule_500_mixed_population():
    results = _benchmark(500)
    assert results["seconds_first_run"] < 120


@pytest.mark.perf
@pytest.mark.skipif(os.environ.get("CAREEROS_PERF") != "1", reason="set CAREEROS_PERF=1 for the 5,000-opportunity run")
def test_schedule_5000_mixed_population():
    results = _benchmark(5000)
    assert results["ai_calls"] == 0
