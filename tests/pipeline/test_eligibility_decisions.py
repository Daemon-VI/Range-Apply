"""Tier 1 decisions are persistent, inspectable, versioned, and never rounded."""

import pytest

from app.pipeline.models import EligibilityDecision, OpportunityState
from app.pipeline.repository import OpportunityRepository


def _co(db_session, jobs, repo):
    job = jobs.make()
    opp, _, _ = OpportunityRepository.resolve_opportunity(db_session, job)
    co, _ = repo.ensure_candidate_opportunity(opp, "test")
    return job, co


@pytest.mark.parametrize(
    "decision, expected_state",
    [
        (EligibilityDecision.ELIGIBLE, OpportunityState.ELIGIBLE),
        (EligibilityDecision.LIKELY, OpportunityState.ELIGIBLE),
        (EligibilityDecision.UNCERTAIN, OpportunityState.UNCERTAIN),
        (EligibilityDecision.REVIEW, OpportunityState.UNCERTAIN),
        (EligibilityDecision.INELIGIBLE, OpportunityState.INELIGIBLE),
    ],
)
def test_decision_moves_state_without_collapsing_uncertain(db_session, jobs, repo, decision, expected_state):
    job, co = _co(db_session, jobs, repo)
    row = repo.record_eligibility(
        co,
        job,
        decision,
        confidence="MEDIUM",
        reason_codes=[f"code:{decision.value.lower()}"],
        matched=[{"constraint": "graduation", "reason": "2027 fits"}],
        failed=[{"constraint": "authorization", "reason": "requires US authorization"}] if decision is EligibilityDecision.INELIGIBLE else [],
        uncertain=[{"constraint": "location", "reason": "JD silent"}] if decision in (EligibilityDecision.UNCERTAIN, EligibilityDecision.REVIEW) else [],
        ruleset_version="tier1-test-v1",
        actor="test",
    )
    repo.commit()
    assert co.state == expected_state.value
    assert co.eligibility_status == decision.value
    assert co.eligibility_decision_id == row.id
    assert row.tenant_id == repo.tenant_id
    assert row.ruleset_version == "tier1-test-v1"
    assert row.job_content_hash == job.content_hash
    assert row.reason_codes == [f"code:{decision.value.lower()}"]
    assert repo.latest_decision(co.id).id == row.id


def test_reevaluation_keeps_history_and_never_regresses_an_active_application(db_session, jobs, repo):
    job, co = _co(db_session, jobs, repo)
    repo.record_eligibility(co, job, EligibilityDecision.UNCERTAIN, "LOW", ["uncertain:location"], [], [], [{"constraint": "location", "reason": "silent"}], "v1", "test")
    repo.record_eligibility(co, job, EligibilityDecision.ELIGIBLE, "HIGH", ["ok:location"], [{"constraint": "location", "reason": "remote"}], [], [], "v2", "test")
    assert [d.decision for d in repo.decisions_for(co.id)] == ["ELIGIBLE", "UNCERTAIN"]
    assert co.state == OpportunityState.ELIGIBLE.value

    repo.transition(co, OpportunityState.QUEUED, "api")
    repo.record_eligibility(co, job, EligibilityDecision.INELIGIBLE, "HIGH", ["blocked:x"], [], [{"constraint": "x", "reason": "y"}], [], "v2", "test")
    repo.commit()
    # Decision recorded and visible, but the queued application is not yanked back.
    assert co.eligibility_status == "INELIGIBLE"
    assert co.state == OpportunityState.QUEUED.value
    assert len(repo.decisions_for(co.id)) == 3
    actions = [e.action for e in repo.list_audit("candidate_opportunity", co.id)]
    assert "eligibility:INELIGIBLE" in actions and "eligibility:UNCERTAIN" in actions


def test_decisions_are_tenant_scoped(db_session, jobs, repo, other_repo):
    job, co = _co(db_session, jobs, repo)
    repo.record_eligibility(co, job, EligibilityDecision.ELIGIBLE, "HIGH", [], [], [], [], "v1", "test")
    repo.commit()
    assert other_repo.latest_decision(co.id) is None
    assert other_repo.decisions_for(co.id) == []
