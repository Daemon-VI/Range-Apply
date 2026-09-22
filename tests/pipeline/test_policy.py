"""Application policy: candidate-controlled admission; priority is absent by design."""

import pytest

from app.core.errors import ValidationFailed
from app.pipeline.models import (
    ApplicationPolicyUpdate,
    EligibilityDecision,
    FitBand,
    Lane,
    TailoringLevel,
)
from app.pipeline.policy import evaluate_admission


def test_defaults_are_aggressive_and_persisted(policy_repo):
    policy = policy_repo.get()
    policy_repo.commit()
    assert policy.enabled_bands == [FitBand.HIGH, FitBand.MEDIUM, FitBand.LOW]
    assert policy.band_thresholds == {"HIGH": 70, "MEDIUM": 45}
    assert policy.minimum_eligibility is EligibilityDecision.UNCERTAIN
    assert policy.lane_by_band["LOW"] is Lane.REVIEW
    assert policy.tailoring_by_band["HIGH"] is TailoringLevel.L2
    assert policy.version == 1
    assert policy_repo.get_row() is not None


def test_admission_cases(policy_repo):
    policy = policy_repo.get()
    assert evaluate_admission(policy, None, FitBand.HIGH, "Acme").reason == "not_evaluated"
    assert evaluate_admission(policy, EligibilityDecision.INELIGIBLE, FitBand.HIGH, "Acme").reason == "ineligible"
    assert evaluate_admission(policy, EligibilityDecision.ELIGIBLE, None, "Acme").reason == "not_scored"

    # Eligible, LOW band, would be low priority: still admitted while LOW is enabled.
    low = evaluate_admission(policy, EligibilityDecision.ELIGIBLE, FitBand.LOW, "Acme")
    assert low.admitted and low.lane is Lane.REVIEW and low.tailoring_level is TailoringLevel.L0

    uncertain = evaluate_admission(policy, EligibilityDecision.UNCERTAIN, FitBand.MEDIUM, "Acme")
    assert uncertain.admitted, "UNCERTAIN is admitted (to review), never treated as ineligible"


def test_policy_update_changes_admission_with_audit_and_version(policy_repo, repo):
    updated = policy_repo.update(
        ApplicationPolicyUpdate(
            enabled_bands=[FitBand.HIGH, FitBand.MEDIUM],
            minimum_eligibility=EligibilityDecision.LIKELY,
            blocked_companies=["Evil Corp"],
            lane_by_band={"HIGH": Lane.AUTO, "MEDIUM": Lane.REVIEW, "LOW": Lane.MANUAL},
            daily_cap=120,
        ),
        actor="api",
    )
    policy_repo.commit()
    assert updated.version == 2 and updated.daily_cap == 120
    assert evaluate_admission(updated, EligibilityDecision.ELIGIBLE, FitBand.LOW, "Acme").reason == "band_disabled:LOW"
    assert evaluate_admission(updated, EligibilityDecision.UNCERTAIN, FitBand.HIGH, "Acme").reason == "below_minimum_eligibility:UNCERTAIN"
    assert evaluate_admission(updated, EligibilityDecision.ELIGIBLE, FitBand.HIGH, "evil corp").reason == "company_blocked"
    assert evaluate_admission(updated, EligibilityDecision.LIKELY, FitBand.HIGH, "Acme").lane is Lane.AUTO

    events = repo.list_audit("application_policy")
    assert [e.action for e in events] == ["updated", "created"]
    assert events[0].before["enabled_bands"] == ["HIGH", "MEDIUM", "LOW"]
    assert events[0].after["enabled_bands"] == ["HIGH", "MEDIUM"]

    same = policy_repo.update(ApplicationPolicyUpdate(daily_cap=120), actor="api")
    assert same.version == 2


def test_invalid_thresholds_are_rejected(policy_repo):
    with pytest.raises(ValidationFailed):
        policy_repo.update(ApplicationPolicyUpdate(band_thresholds={"HIGH": 40, "MEDIUM": 45}), actor="api")


def test_policies_are_per_tenant(policy_repo, db_session, other_tenant_id):
    from app.pipeline.repository import PolicyRepository

    policy_repo.update(ApplicationPolicyUpdate(daily_cap=7), actor="api")
    policy_repo.commit()
    other = PolicyRepository(db_session, other_tenant_id).get()
    assert other.daily_cap == 50 and other.tenant_id == other_tenant_id
