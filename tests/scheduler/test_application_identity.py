"""ApplicationEngine identity: tenant + opportunity, with job ids resolving to it."""

from app.application.engine import ApplicationEngine
from app.application.models import ApplicationStatus
from app.pipeline.repository import OpportunityRepository


def test_get_or_create_by_opportunity_reuses_across_source_jobs(db_session, tenant_id, opportunities, jobs):
    co = opportunities.make(title="Backend Engineer", fit_score=80, company="Acme", location="Remote")
    second = jobs.make(title="Backend Engineer", company="Acme", location="Remote (India)", source="LEVER")
    opp, _, _ = OpportunityRepository.resolve_opportunity(db_session, second)
    db_session.commit()
    assert opp.id == co.opportunity_id
    engine = ApplicationEngine()
    first = engine.get_or_create(db_session, co.opportunity.canonical_job_id, tenant_id=tenant_id)
    again = engine.get_or_create(db_session, second.id, tenant_id=tenant_id)
    assert first.id == again.id, "the legacy job-id path resolves to the same attempt"
    assert again.opportunity_id == opp.id and again.tenant_id == tenant_id
    by_opp = engine.get_or_create(db_session, second.id, tenant_id=tenant_id, opportunity_id=opp.id, candidate_opportunity_id=co.id)
    assert by_opp.id == first.id and by_opp.candidate_opportunity_id == co.id


def test_attempts_are_tenant_scoped(db_session, tenant_id, other_tenant_id, opportunities):
    co = opportunities.make(fit_score=80, company="Acme")
    engine = ApplicationEngine()
    mine = engine.get_or_create(db_session, co.opportunity.canonical_job_id, tenant_id=tenant_id, opportunity_id=co.opportunity_id)
    assert mine.tenant_id == tenant_id
    # Another tenant asking about the same job does not get this tenant's attempt.
    from app.application.database.models import ApplicationRow

    theirs = db_session.query(ApplicationRow).filter_by(tenant_id=other_tenant_id, opportunity_id=co.opportunity_id).first()
    assert theirs is None


def test_scheduler_attempt_then_engine_prepare_with_preparation(db_session, tenant_id, scheduler, opportunities, answered_bank):
    co = opportunities.make(fit_score=90, company="Acme")
    run = scheduler.run(prepare=True)
    attempt = scheduler.attempts.get_for_opportunity(co.opportunity_id)
    assert run.ready_for_execution == 1 and attempt.status == ApplicationStatus.READY.value
    engine = ApplicationEngine()
    row = engine.prepare(db_session, co.opportunity.canonical_job_id, tenant_id=tenant_id, opportunity_id=co.opportunity_id, preparation_id=attempt.preparation_id)
    assert row.id == attempt.id and row.status == ApplicationStatus.READY.value
    assert row.preparation_id == attempt.preparation_id
