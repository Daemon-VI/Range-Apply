"""Geographic targeting end to end: discovery -> normalization -> eligibility -> fit -> priority -> admission.

The seeded tenant's profile records "Hyderabad, India" and no location
preference, so the target comes from the profile.
"""

import asyncio
import uuid

from app.intelligence.database.models import MatchRunRow
from app.intelligence.services.factory import build_orchestrator
from app.intelligence.services.match_persistence import run_matching
from app.jobs.database.models import DiscoveryRunRow, JobRow, SourceHealthRow
from app.jobs.models.enums import JobSourceType
from app.jobs.pipeline.discovery_service import JobDiscoveryService
from app.models.preference import Preference
from app.pipeline.database.models import CandidateOpportunityRow, OpportunityRow
from app.pipeline.models import AdmissionReason
from app.pipeline.sync import sync_run
from app.services.career_brain import CareerBrainService
from tests.discovery.conftest import fake_source, make_raw
from tests.scheduler.conftest import decisions_by_co

DESCRIPTION = "<p>Build backend services in Python with PostgreSQL and Docker. Bachelor's degree in Computer Science.</p>"


def _cleanup(db, slug: str, run_ids: list) -> None:
    db.rollback()
    companies = {job.company for job in db.query(JobRow).filter(JobRow.source_identifier == slug).all()}
    for opportunity in db.query(OpportunityRow).filter(OpportunityRow.company.in_(companies)).all():
        db.delete(opportunity)
    for job in db.query(JobRow).filter(JobRow.source_identifier == slug).all():
        db.delete(job)
    for run_id in run_ids:
        run = db.get(MatchRunRow, run_id)
        if run is not None:
            db.delete(run)
    db.query(DiscoveryRunRow).filter(DiscoveryRunRow.source_identifier == slug).delete()
    db.query(SourceHealthRow).filter(SourceHealthRow.source_identifier == slug).delete()
    db.commit()


def test_complete_flow_keeps_us_postings_out_of_admission(db_session, tenant_id, evidence, scheduler):
    slug = f"geoflow{uuid.uuid4().hex[:8]}"
    postings = [
        make_raw(f"{slug}-hyd", title="Software Engineer", company=slug, location="Hyderabad, Telangana, India", content=DESCRIPTION),
        make_raw(f"{slug}-sf", title="Software Engineer II", company=slug, location="San Francisco, CA", content=DESCRIPTION),
        make_raw(f"{slug}-usr", title="Backend Engineer", company=slug, location="US Remote", content=DESCRIPTION),
    ]
    service = JobDiscoveryService()
    service.register_source(JobSourceType.OTHER, fake_source(jobs=postings))
    run_ids: list = []
    try:
        # Unfiltered on purpose: the later stages must hold on their own.
        asyncio.run(service.run_discovery(db_session, JobSourceType.OTHER, slug, close_missing=False))
        jobs = {job.location: job for job in db_session.query(JobRow).filter(JobRow.source_identifier == slug).all()}
        assert set(jobs) == {"Hyderabad, Telangana, India", "San Francisco, CA", "US Remote"}

        brain = CareerBrainService(db=db_session, tenant_id=tenant_id)
        brain.load()
        match_run = run_matching(db_session, build_orchestrator(brain), job_ids=[job.id for job in jobs.values()], trigger="test", tenant_id=tenant_id)
        run_ids.append(match_run.id)
        sync_run(db_session, tenant_id, match_run.id)

        by_job = {co.opportunity.canonical_job_id: co for co in db_session.query(CandidateOpportunityRow).filter(CandidateOpportunityRow.tenant_id == tenant_id).all()}
        hyderabad, us, us_remote = (by_job[jobs[location].id] for location in ("Hyderabad, Telangana, India", "San Francisco, CA", "US Remote"))

        assert hyderabad.eligibility_status in ("ELIGIBLE", "LIKELY")
        assert us.eligibility_status == "UNCERTAIN" and us_remote.eligibility_status == "UNCERTAIN"
        assert hyderabad.fit_score > us.fit_score and hyderabad.fit_score > us_remote.fit_score
        assert hyderabad.priority_score > us.priority_score
        assert hyderabad.policy_admitted
        assert not us.policy_admitted and us.policy_reason.startswith("outside_target_geography")
        assert not us_remote.policy_admitted

        decisions = decisions_by_co(scheduler.preview())
        assert decisions[us.id].code is AdmissionReason.OUTSIDE_TARGET_GEOGRAPHY
        assert decisions[us_remote.id].code is AdmissionReason.OUTSIDE_TARGET_GEOGRAPHY
        assert decisions[hyderabad.id].code is AdmissionReason.ADMITTED
    finally:
        _cleanup(db_session, slug, run_ids)


def test_irrelevant_us_posting_no_longer_takes_the_company_cooldown_slot(db_session, tenant_id, evidence, scheduler, opportunities):
    us = opportunities.make(title="Platform Engineer", fit_score=95, company="SlotCo", location="San Francisco, CA", remote_type="HYBRID")
    hyderabad = opportunities.make(title="Software Engineer", fit_score=60, company="SlotCo", location="Hyderabad, Telangana, India", remote_type="UNKNOWN")
    us.priority_score, hyderabad.priority_score = 95, 60
    db_session.commit()

    decisions = decisions_by_co(scheduler.preview())
    assert decisions[us.id].code is AdmissionReason.OUTSIDE_TARGET_GEOGRAPHY
    assert decisions[hyderabad.id].code is AdmissionReason.ADMITTED, "the higher-priority US posting used to take the slot and cool the company down"
    assert scheduler.run().admitted == 1


def _technical_targets(evidence) -> None:
    evidence.upsert_profile({}, Preference(target_roles_tier1=["Software Engineer", "Backend Engineer"]).model_dump(), actor="test")
    evidence.commit()


def test_unrelated_low_role_cannot_take_the_company_slot_from_a_medium_technical_role(db_session, tenant_id, evidence, scheduler, opportunities):
    _technical_targets(evidence)
    fresh_unrelated = opportunities.make(title="Proprietary Content Creator", fit_score=40, company="BandCo", location="Hyderabad, Telangana, India", remote_type="UNKNOWN")
    engineering = opportunities.make(title="Software Engineer", fit_score=53, company="BandCo", location="Hyderabad, Telangana, India", remote_type="UNKNOWN")
    fresh_unrelated.priority_score, engineering.priority_score = 53, 46
    db_session.commit()

    assert decisions_by_co(scheduler.preview())[fresh_unrelated.id].code is AdmissionReason.IRRELEVANT_ROLE
    scheduler.run()
    db_session.refresh(fresh_unrelated)
    db_session.refresh(engineering)
    assert engineering.scheduler_code == AdmissionReason.ADMITTED.value
    assert fresh_unrelated.scheduler_code == AdmissionReason.IRRELEVANT_ROLE.value


def test_company_slot_goes_to_the_more_relevant_role_before_band_and_priority(db_session, tenant_id, evidence, scheduler, opportunities):
    _technical_targets(evidence)
    weak = opportunities.make(title="Implementation Consultant", fit_score=66, company="OrderCo", location="Hyderabad, Telangana, India", remote_type="UNKNOWN")
    technical = opportunities.make(title="Backend Engineer", fit_score=50, company="OrderCo", location="Hyderabad, Telangana, India", remote_type="UNKNOWN")
    weak.priority_score, technical.priority_score = 90, 40
    db_session.commit()

    scheduler.run()
    db_session.refresh(weak)
    db_session.refresh(technical)
    assert technical.scheduler_code == AdmissionReason.ADMITTED.value
    assert weak.scheduler_code == AdmissionReason.COOLDOWN_ACTIVE.value, "a weak role is still admissible, just never ahead of a relevant one at the same company"
