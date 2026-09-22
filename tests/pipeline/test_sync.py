"""Match -> candidate opportunity sync: idempotent, volume-preserving."""

from app.pipeline.models import ApplicationPolicyUpdate, FitBand, OpportunityState
from app.pipeline.repository import PolicyRepository
from app.pipeline.sync import sync_match, sync_run


def test_sync_match_records_decision_fit_priority_and_admission(db_session, jobs, matches, tenant_id, repo):
    job = jobs.make(posted_days_ago=1, deadline_in_days=3)
    match = matches.make(job, fit_score=82, eligibility_status="ELIGIBLE")

    _, result = sync_match(db_session, tenant_id, match)
    db_session.commit()
    co = result.candidate_opportunity
    assert result.opportunity_created and result.candidate_created and result.admitted
    assert co.state == OpportunityState.ELIGIBLE.value
    assert co.eligibility_status == "ELIGIBLE"
    assert co.fit_score == 82 and co.fit_band == FitBand.HIGH.value and co.match_id == match.id
    assert co.priority_score is not None and co.priority_score > 60
    assert co.policy_admitted is True and co.policy_reason == "admitted:ELIGIBLE:HIGH"
    decision = repo.latest_decision(co.id)
    assert decision.ruleset_version == "tier1-from-match-v1"
    assert decision.reason_codes[0].startswith("ok:graduation-year")

    audit_before = len(repo.list_audit(limit=1000))
    _, again = sync_match(db_session, tenant_id, match)
    db_session.commit()
    assert not again.opportunity_created and not again.candidate_created
    assert len(repo.list_audit(limit=1000)) == audit_before, "unchanged re-sync writes no audit noise"


def test_uncertain_and_ineligible_are_distinct_and_low_band_is_not_ineligible(db_session, jobs, matches, tenant_id, repo):
    uncertain_job = jobs.make(title="Data Engineer")
    ineligible_job = jobs.make(title="Staff Engineer")
    low_job = jobs.make(title="QA Engineer", posted_days_ago=40)
    uncertain = matches.make(uncertain_job, 60, "UNCERTAIN", uncertainties=["Work authorization: JD did not state requirements"])
    ineligible = matches.make(ineligible_job, 95, "INELIGIBLE", blocking_reasons=["Graduation year: requires graduating by 2025"])
    low = matches.make(low_job, 30, "ELIGIBLE")

    results = {m.id: sync_match(db_session, tenant_id, m)[1].candidate_opportunity for m in (uncertain, ineligible, low)}
    db_session.commit()

    assert results[uncertain.id].state == "UNCERTAIN" and results[uncertain.id].policy_admitted is True
    assert repo.latest_decision(results[uncertain.id].id).uncertain_constraints[0]["reason"].startswith("Work authorization")
    assert results[ineligible.id].state == "INELIGIBLE" and results[ineligible.id].policy_admitted is False
    assert results[ineligible.id].policy_reason == "ineligible"
    low_co = results[low.id]
    assert low_co.state == "ELIGIBLE" and low_co.fit_band == "LOW" and low_co.policy_admitted is True
    assert low_co.priority_score < results[uncertain.id].priority_score

    # Disabling LOW changes admission (candidate's choice) but never eligibility or state.
    PolicyRepository(db_session, tenant_id).update(ApplicationPolicyUpdate(enabled_bands=[FitBand.HIGH, FitBand.MEDIUM]), "api")
    db_session.commit()
    _, re = sync_match(db_session, tenant_id, low)
    db_session.commit()
    assert re.candidate_opportunity.policy_admitted is False
    assert re.candidate_opportunity.policy_reason == "band_disabled:LOW"
    assert re.candidate_opportunity.state == "ELIGIBLE"
    assert re.candidate_opportunity.eligibility_status == "ELIGIBLE"


def test_sync_run_covers_every_match_and_is_isolated_per_tenant(db_session, jobs, matches, tenant_id, other_tenant_id, repo, other_repo):
    for i in range(3):
        matches.make(jobs.make(title=f"Engineer {i}"), fit_score=50 + i * 10)
    report = sync_run(db_session, tenant_id, matches.run.id)
    assert report.synced == 3 and report.candidate_created == 3 and report.errors == []
    assert report.admitted == 3
    assert repo.list_candidate_opportunities()[1] == 3
    assert other_repo.list_candidate_opportunities()[1] == 0

    second = sync_run(db_session, tenant_id, matches.run.id)
    assert second.synced == 3 and second.candidate_created == 0 and second.opportunities_created == 0

    other = sync_run(db_session, other_tenant_id, matches.run.id)
    assert other.candidate_created == 3 and other.opportunities_created == 0, "shared opportunities are reused"
    assert other_repo.list_candidate_opportunities()[1] == 3
