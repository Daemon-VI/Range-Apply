"""High-volume deterministic preparation: 1,000 opportunities always, 5,000 opt-in.

Measures wall time, DB write statements, peak traced memory and AI calls
for L0 and deterministic L1 packages built from real Evidence Graph data.
"""

import os
import time
import tracemalloc
import uuid

import pytest
from sqlalchemy import event

from app.career.database.models import TenantRow
from app.career.importer import SeedImporter
from app.career.models import AnswerBankEntryCreate, AnswerStatus
from app.career.repository import EvidenceRepository
from app.config import settings
from app.core.timeutils import db_now
from app.database import get_engine, get_session_factory
from app.intelligence.database.models import JobMatchRow, MatchRunRow, RequirementAssessmentRow
from app.intelligence.services.match_persistence import ensure_policy
from app.jobs.database.models import JobRow
from app.pipeline.database.models import CandidateOpportunityRow, OpportunityJobRow, OpportunityRow
from app.pipeline.models import TailoringLevel
from app.pipeline.repository import PolicyRepository
from app.preparation.database.models import ApplicationPreparationRow
from app.preparation.service import PreparationService
from tests.discovery.conftest import WriteCounter
from tests.preparation.conftest import FACT_ANSWERS

TITLES = ["Backend Engineer", "Data Engineer", "Platform Engineer", "ML Engineer", "Software Engineer", "SDE I"]
REQS = [("Python", ["skill-python"]), ("Go", ["skill-go"]), ("SQL", ["skill-sql"]), ("FastAPI", ["skill-fastapi"]), ("Docker", ["skill-docker"])]


def _seed(session, tenant_id: str, n: int) -> list[str]:
    """Bulk-create n jobs, matches, assessments, opportunities and admitted candidate rows."""
    ensure_policy(session)
    run = MatchRunRow(policy_version="v1", engine_version="1.1.0", career_brain_version="v1", started_at=db_now(), completed_at=db_now(), status="COMPLETED", trigger="perf", errors=[])
    session.add(run)
    session.flush()
    suffix = uuid.uuid4().hex[:6]
    co_ids = []
    jobs, matches, opps, cos, links, assessments = [], [], [], [], [], []
    for i in range(n):
        job = JobRow(
            id=str(uuid.uuid4()),
            canonical_key=f"perf-{suffix}-{i}",
            source="GREENHOUSE",
            source_job_id=f"perf-{suffix}-{i}",
            company=f"PerfCo {suffix} {i % 97}",
            title=TITLES[i % len(TITLES)],
            original_title=TITLES[i % len(TITLES)],
            description="Python and Go backend role.",
            original_description="Python and Go backend role.",
            location="Remote",
            remote_type="REMOTE",
            source_url=f"https://perf.example/{suffix}/{i}",
            content_hash=uuid.uuid4().hex,
            processing_status="NORMALIZED",
            job_status="ACTIVE",
            first_seen_at=db_now(),
            last_seen_at=db_now(),
        )
        match = JobMatchRow(
            id=str(uuid.uuid4()),
            job_id=job.id,
            run_id=run.id,
            job_canonical_key=job.canonical_key,
            job_content_hash=job.content_hash,
            eligibility_status="ELIGIBLE",
            eligibility_confidence="HIGH",
            fit_score=30 + (i * 7) % 65,
            priority="P1",
            match_type="CORE_MATCH",
            confidence="HIGH",
            explanation="perf",
            evaluated_at=db_now(),
        )
        opp = OpportunityRow(id=str(uuid.uuid4()), identity_key=f"perf-{suffix}-{i}", canonical_job_id=job.id, company=job.company, title=job.title, location_bucket="remote", status="OPEN")
        co = CandidateOpportunityRow(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            opportunity_id=opp.id,
            state="ELIGIBLE",
            eligibility_status="ELIGIBLE",
            match_id=match.id,
            fit_score=match.fit_score,
            fit_band="HIGH" if match.fit_score > 70 else "MEDIUM" if match.fit_score >= 45 else "LOW",
            policy_admitted=True,
            policy_reason="admitted:perf",
        )
        jobs.append(job)
        matches.append(match)
        opps.append(opp)
        cos.append(co)
        links.append(OpportunityJobRow(opportunity_id=opp.id, job_id=job.id, linked_by="new"))
        for name, refs in REQS[: 2 + i % 3]:
            assessments.append(RequirementAssessmentRow(job_match_id=match.id, requirement_name=name, requirement_category="TECHNICAL_SKILL", requirement_strictness="REQUIRED", status="MATCHED", evidence_strength="DIRECT_VERIFIED", evidence_references=refs, confidence="HIGH", weight=1.0, contribution=10.0))
        co_ids.append(co.id)
    # Insert in FK dependency order; SQLite enforces foreign keys immediately.
    for batch in (jobs, matches, opps, cos, links, assessments):
        session.add_all(batch)
        session.flush()
    session.commit()
    return co_ids


def _cleanup(session, tenant_id: str, suffix_free_company_prefix: str = "PerfCo") -> None:
    session.rollback()
    row = session.get(TenantRow, tenant_id)
    if row is not None:
        session.delete(row)
        session.commit()
    for opp in session.query(OpportunityRow).filter(OpportunityRow.company.like(f"{suffix_free_company_prefix}%")).all():
        session.delete(opp)
    for job in session.query(JobRow).filter(JobRow.company.like(f"{suffix_free_company_prefix}%")).all():
        session.delete(job)
    for run in session.query(MatchRunRow).filter(MatchRunRow.trigger == "perf").all():
        session.delete(run)
    session.commit()


def _benchmark(n: int) -> dict:
    session = get_session_factory()()
    tenant_id = f"perf-p4-{uuid.uuid4().hex[:6]}"
    counter = WriteCounter()
    engine = get_engine()
    try:
        session.add(TenantRow(id=tenant_id, name="perf"))
        session.commit()
        repo = EvidenceRepository(session, tenant_id)
        SeedImporter(repo).import_file(settings.career_data_path)
        for category, question, answer in FACT_ANSWERS:
            repo.create_answer(AnswerBankEntryCreate(category=category, question=question, answer=answer, status=AnswerStatus.APPROVED), "perf")
        repo.commit()
        PolicyRepository(session, tenant_id).get()
        session.commit()
        co_ids = _seed(session, tenant_id, n)

        results = {"opportunities": n}
        for level in (TailoringLevel.L0, TailoringLevel.L1):
            service = PreparationService(session, tenant_id, actor="perf")
            event.listen(engine, "after_cursor_execute", counter)
            writes_before = counter.writes
            tracemalloc.start()
            started = time.perf_counter()
            report = service.prepare_many(co_ids, level=level, force=True, commit_every=200)
            seconds = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            event.remove(engine, "after_cursor_execute", counter)
            assert report.errors == [], report.errors[:3]
            assert report.created == n and report.ai_calls == 0
            assert report.by_status.get("READY") == n
            results[level.value] = {
                "seconds": round(seconds, 2),
                "per_minute": round(n / max(seconds, 1e-6) * 60),
                "db_writes": counter.writes - writes_before,
                "peak_memory_mb": round(peak / 1024 / 1024, 1),
                "ai_calls": report.ai_calls,
            }
        stored = session.query(ApplicationPreparationRow).filter_by(tenant_id=tenant_id).count()
        assert stored == 2 * n
        print("\nPREPARATION BENCHMARK:", results)
        return results
    finally:
        _cleanup(session, tenant_id)
        session.close()


@pytest.mark.perf
def test_prepare_1000_deterministic_zero_ai():
    results = _benchmark(1000)
    assert results["L0"]["ai_calls"] == 0 and results["L1"]["ai_calls"] == 0
    assert results["L0"]["seconds"] < 300


@pytest.mark.perf
@pytest.mark.skipif(os.environ.get("CAREEROS_PERF") != "1", reason="set CAREEROS_PERF=1 for the 5,000-opportunity run")
def test_prepare_5000_deterministic_zero_ai():
    results = _benchmark(5000)
    assert results["L0"]["ai_calls"] == 0 and results["L1"]["ai_calls"] == 0
