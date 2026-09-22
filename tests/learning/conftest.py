"""Fixtures for Phase 11 learning tests.

``history()`` writes an executed-application history straight into the
tables the engine reads (job, opportunity, candidate opportunity, attempt,
execution run, preparation, outcome events) in one commit, so thousands of
rows cost seconds. The scheduler / sync integration tests use the real
Phase 2/5 fixtures instead.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.application.database.models import ApplicationRow
from app.career.database.models import PositioningVariantRow
from app.execution.database.models import ExecutionRunRow
from app.jobs.database.models import JobRow
from app.learning.engine import LearningEngine
from app.learning.models import TenantLearningSettings
from app.main import app
from app.pipeline.database.models import CandidateOpportunityRow, OpportunityRow
from app.pipeline.policy import company_key
from app.preparation.database.models import ApplicationPreparationRow
from app.signals.database.models import OutcomeEventRow
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

T0 = datetime(2026, 3, 1, 9, 0, 0)


@pytest.fixture(autouse=True)
def _no_ai():
    from app.ai.gateway import reset_gateway

    reset_gateway(None)
    yield
    reset_gateway(None)


@pytest.fixture
def engine(db_session, tenant_id) -> LearningEngine:
    return LearningEngine(db_session, tenant_id, TenantLearningSettings(), actor="test")


def spec(company="Acme", title="Backend Engineer", source="GREENHOUSE", fit_band="HIGH", fit_score=85, lane="REVIEW", level="L1", cover_letter="TEMPLATE", role_family: Optional[str] = None, executor="MOCK", verification="VERIFIED", run_status="VERIFIED", events: Optional[list] = None, submitted_offset_hours: float = 0.0, prep: bool = True, status: Optional[str] = None) -> dict[str, Any]:
    """``events``: list of (outcome, evidence, days_after_submit[, observed_days_after])."""
    return {"company": company, "title": title, "source": source, "fit_band": fit_band, "fit_score": fit_score, "lane": lane, "level": level, "cover_letter": cover_letter, "role_family": role_family, "executor": executor, "verification": verification, "run_status": run_status, "events": events or [], "submitted_offset_hours": submitted_offset_hours, "prep": prep, "status": status}


def history(session, tenant_id: str, specs: list[dict], start: datetime = T0, spacing_hours: float = 6.0) -> list[ApplicationRow]:
    """Persist one executed application per spec; returns the attempts in order."""
    attempts: list[ApplicationRow] = []
    variants: dict[str, PositioningVariantRow] = {}
    for i, s in enumerate(specs):
        submitted = start + timedelta(hours=i * spacing_hours + s["submitted_offset_hours"])
        uid = uuid.uuid4().hex[:10]
        job = JobRow(canonical_key=f"lrn-{uid}", source=s["source"], source_job_id=f"lrn-{uid}", company=s["company"], title=s["title"], original_title=s["title"], description="", original_description="", location="Remote", remote_type="REMOTE", source_url=f"https://example.com/{s['source'].lower()}/{uid}", application_url=f"https://example.com/{s['source'].lower()}/{uid}/apply", content_hash=uid, first_seen_at=submitted - timedelta(days=2), last_seen_at=submitted, job_status="ACTIVE", processing_status="MATCHED")
        session.add(job)
        session.flush()
        opp = OpportunityRow(identity_key=f"lrn-{uid}", canonical_job_id=job.id, company=s["company"], company_key=company_key(s["company"]), title=s["title"], location_bucket="remote", status="OPEN", first_seen_at=job.first_seen_at, last_seen_at=submitted)
        session.add(opp)
        session.flush()
        co = CandidateOpportunityRow(tenant_id=tenant_id, opportunity_id=opp.id, state="SUBMITTED", eligibility_status="ELIGIBLE", fit_score=s["fit_score"], fit_band=s["fit_band"], policy_admitted=True, admission_policy_version=1, fit_policy_version=1, gate_ruleset_version="tier1-gates-v1")
        session.add(co)
        session.flush()
        verified = s["verification"] == "VERIFIED"
        status = s["status"] or ("VERIFIED" if verified else ("UNCERTAIN" if s["run_status"] == "UNKNOWN" else "SUBMITTED"))
        attempt = ApplicationRow(job_id=job.id, tenant_id=tenant_id, opportunity_id=opp.id, candidate_opportunity_id=co.id, status=status, attempt_number=1, lane=s["lane"], tailoring_level=s["level"], cap_day="2026-03-01", cap_week="2026-W09", reserved_at=submitted - timedelta(hours=2), submitted_at=submitted if s["run_status"] != "UNKNOWN" else None, verified_at=submitted + timedelta(minutes=5) if verified else None, created_at=submitted - timedelta(hours=3), updated_at=submitted)
        session.add(attempt)
        session.flush()
        co.application_id = attempt.id
        prep = None
        if s["prep"]:
            variant = None
            if s["role_family"]:
                variant = variants.get(s["role_family"])
                if variant is None:
                    variant = PositioningVariantRow(tenant_id=tenant_id, role_family=s["role_family"], name=f"{s['role_family']} variant", headline="", summary="")
                    session.add(variant)
                    session.flush()
                    variants[s["role_family"]] = variant
            prep = ApplicationPreparationRow(tenant_id=tenant_id, candidate_opportunity_id=co.id, opportunity_id=opp.id, job_id=job.id, version=1, tailoring_level=s["level"], lane=s["lane"], cover_letter_mode=s["cover_letter"], positioning_variant_id=variant.id if variant else None, status="READY", validation_status="PASSED", input_fingerprint=uid, inputs={}, evidence_keys=[], created_at=submitted - timedelta(hours=2))
            session.add(prep)
            session.flush()
            attempt.preparation_id = prep.id
        run = ExecutionRunRow(tenant_id=tenant_id, application_id=attempt.id, preparation_id=prep.id if prep else None, candidate_opportunity_id=co.id, opportunity_id=opp.id, executor_kind=s["executor"], executor_version="test", idempotency_key=f"{tenant_id}:{attempt.id}:1:1", run_number=1, status=s["run_status"], outcome="SUBMITTED" if s["run_status"] != "UNKNOWN" else "UNKNOWN", submit_invoked=True, started_at=submitted - timedelta(minutes=10), finished_at=submitted, verification_status=s["verification"], verification_method="application_id" if verified else None)
        session.add(run)
        session.flush()
        attempt.last_execution_id = run.id
        for seq, event in enumerate(s["events"], start=1):
            outcome, evidence_strength, days_after = event[0], event[1], event[2]
            observed_days = event[3] if len(event) > 3 else days_after
            session.add(OutcomeEventRow(tenant_id=tenant_id, application_id=attempt.id, outcome=outcome, evidence=evidence_strength, origin="rules", category=None, event_at=submitted + timedelta(days=days_after), time_basis="external", observed_at=submitted + timedelta(days=observed_days), sequence=seq, dedupe_key=f"{uid}:{attempt.id}:{outcome}:{seq}", actor="test", rules_version="outcome-rules-v1"))
        attempts.append(attempt)
    session.commit()
    for a in attempts:
        session.refresh(a)
    return attempts


def mixed_specs(n: int, companies: int = 10, seed_events: bool = True) -> list[dict]:
    """A deterministic mixed population: sources, bands, lanes, methods and outcomes."""
    out = []
    sources = ("GREENHOUSE", "LEVER", "ASHBY")
    bands = ("HIGH", "MEDIUM", "LOW")
    executors = ("PLAYWRIGHT_LOCAL", "BROWSER_EXTENSION", "MANUAL")
    for i in range(n):
        events = []
        if seed_events:
            r = i % 10
            if r in (0, 1, 2):
                events = [("APPLICATION_RECEIVED", "STRONG", 1), ("REJECTED", "STRONG", 7)]
            elif r == 3:
                events = [("APPLICATION_RECEIVED", "STRONG", 1), ("INTERVIEW_REQUESTED", "STRONG", 5)]
            elif r == 4:
                events = [("ASSESSMENT_REQUESTED", "STRONG", 3)]
            elif r == 5:
                events = [("APPLICATION_RECEIVED", "MODERATE", 2)]
            elif r == 6:
                events = [("REJECTED", "WEAK", 4)]
        verification = "UNKNOWN" if i % 13 == 0 else ("LIKELY" if i % 7 == 0 else "VERIFIED")
        out.append(spec(company=f"Company {i % companies}", title="Backend Engineer" if i % 2 else "Data Engineer", source=sources[i % 3], fit_band=bands[i % 3], fit_score=(90, 60, 30)[i % 3], lane="AUTO" if i % 4 else "REVIEW", level=("L0", "L1", "L2")[i % 3], cover_letter=("DISABLED", "TEMPLATE", "LIGHT")[i % 3], role_family=("backend", "data")[i % 2], executor=executors[i % 3], verification=verification, run_status="UNKNOWN" if verification == "UNKNOWN" else "VERIFIED" if verification == "VERIFIED" else "SUBMITTED", events=events))
    return out


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


__all__ = ["T0", "history", "mixed_specs", "spec"]
