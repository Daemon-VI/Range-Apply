"""Policy correctness matrix.

Every row is one combination of eligibility × band setting × fit × priority ×
caps × cool-down × blocklist × duplicates × preparation outcome, with the
single reason code the scheduler must produce. Priority appears in the inputs
only to prove it never changes the verdict.
"""

import pytest

from app.application.models import ApplicationStatus
from app.pipeline.models import AdmissionReason, OpportunityState
from app.pipeline.repository import OpportunityRepository
from app.preparation.service import PreparationService
from tests.scheduler.conftest import decisions_by_co, fake_attempt, set_policy

CASES = [
    # id, eligibility, fit, priority, policy changes, prior, expected
    ("eligible-high", "ELIGIBLE", 90, 90, {}, None, AdmissionReason.ADMITTED),
    ("eligible-low-low-priority", "ELIGIBLE", 5, 0, {}, None, AdmissionReason.ADMITTED),
    ("likely-medium", "LIKELY", 60, 50, {}, None, AdmissionReason.ADMITTED),
    ("uncertain-default-min", "UNCERTAIN", 60, 50, {}, None, AdmissionReason.ADMITTED),
    ("uncertain-below-min", "UNCERTAIN", 60, 50, {"minimum_eligibility": "LIKELY"}, None, AdmissionReason.BELOW_MINIMUM_ELIGIBILITY),
    ("ineligible-high-fit-high-priority", "INELIGIBLE", 99, 99, {}, None, AdmissionReason.INELIGIBLE),
    ("band-disabled-low", "ELIGIBLE", 10, 95, {"enabled_bands": ["HIGH", "MEDIUM"]}, None, AdmissionReason.BAND_DISABLED),
    ("band-disabled-high", "ELIGIBLE", 95, 95, {"enabled_bands": ["LOW"]}, None, AdmissionReason.BAND_DISABLED),
    ("below-fit-floor", "ELIGIBLE", 40, 95, {"minimum_fit_score": 50}, None, AdmissionReason.BELOW_FIT_THRESHOLD),
    ("at-fit-floor", "ELIGIBLE", 50, 5, {"minimum_fit_score": 50}, None, AdmissionReason.ADMITTED),
    ("not-scored", "ELIGIBLE", None, None, {}, None, AdmissionReason.NOT_SCORED),
    ("not-evaluated", None, 80, 50, {}, None, AdmissionReason.NOT_EVALUATED),
    ("blocked-company", "ELIGIBLE", 90, 90, {"blocked_companies": ["{company}"]}, None, AdmissionReason.COMPANY_BLOCKED),
    ("blocked-beats-ineligible-order", "INELIGIBLE", 90, 90, {"blocked_companies": ["{company}"]}, None, AdmissionReason.INELIGIBLE),
    ("daily-cap-paused", "ELIGIBLE", 90, 90, {"daily_cap": 0}, None, AdmissionReason.DAILY_CAP_REACHED),
    ("weekly-cap-paused", "ELIGIBLE", 90, 90, {"weekly_cap": 0}, None, AdmissionReason.WEEKLY_CAP_REACHED),
    ("cooldown-same-company", "ELIGIBLE", 90, 90, {"cooldown_days": 30}, "submitted-other", AdmissionReason.COOLDOWN_ACTIVE),
    ("cooldown-elapsed", "ELIGIBLE", 90, 90, {"cooldown_days": 1}, "submitted-other", AdmissionReason.ADMITTED),
    ("cooldown-disabled", "ELIGIBLE", 90, 90, {"cooldown_days": 0}, "submitted-other", AdmissionReason.ADMITTED),
    ("duplicate-title", "ELIGIBLE", 90, 90, {"cooldown_days": 0}, "submitted-same-title", AdmissionReason.DUPLICATE_APPLICATION),
    ("already-submitted", "ELIGIBLE", 90, 90, {}, "submitted-self", AdmissionReason.ALREADY_SUBMITTED),
    ("already-in-progress", "ELIGIBLE", 90, 90, {}, "in-flight-self", AdmissionReason.ALREADY_IN_PROGRESS),
    ("in-progress-then-band-disabled", "ELIGIBLE", 90, 90, {"enabled_bands": ["LOW"]}, "in-flight-self", AdmissionReason.ALREADY_IN_PROGRESS),
    ("released-then-readmissible", "ELIGIBLE", 90, 90, {}, "released-self", AdmissionReason.ADMITTED),
    ("skipped", "ELIGIBLE", 90, 90, {}, "skipped", AdmissionReason.USER_BLOCKED),
    ("closed", "ELIGIBLE", 90, 90, {}, "closed", AdmissionReason.OPPORTUNITY_CLOSED),
    ("prep-needs-user-input", "ELIGIBLE", 90, 90, {}, "prep-needs-input", AdmissionReason.NEEDS_USER_INPUT),
    ("prep-needs-input-but-ineligible", "INELIGIBLE", 90, 90, {}, "prep-needs-input", AdmissionReason.INELIGIBLE),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_policy_matrix(case, db_session, tenant_id, scheduler, opportunities, evidence):
    _, eligibility, fit, priority, changes, prior, expected = case
    co = opportunities.make(title="Backend Engineer", fit_score=fit if fit is not None else 80, company="Matrix", eligibility="ELIGIBLE")
    if prior == "prep-needs-input":
        # No answer bank in this fixture: the fact questions need the candidate.
        PreparationService(db_session, tenant_id).prepare(co.id, force=True)
    co.eligibility_status = eligibility
    if fit is None:
        co.fit_score = None
        co.fit_band = None
    co.priority_score = priority
    db_session.commit()
    company = co.opportunity.company
    changes = {k: ([v.replace("{company}", company) for v in val] if isinstance(val, list) else val) for k, val in ((k, v) for k, v in changes.items())}
    if changes:
        set_policy(db_session, tenant_id, **changes)

    if prior == "submitted-other":
        other = opportunities.make(title="Data Scientist", fit_score=70, company="Matrix")
        fake_attempt(db_session, tenant_id, other, ApplicationStatus.SUBMITTED, submitted_days_ago=1.5)
    elif prior == "submitted-same-title":
        # Same title, same company, another location bucket: a different opportunity.
        other = opportunities.make(title="Backend Engineer", fit_score=70, company="Matrix", location="Berlin", remote_type="ONSITE")
        fake_attempt(db_session, tenant_id, other, ApplicationStatus.SUBMITTED, submitted_days_ago=1)
    elif prior == "submitted-self":
        fake_attempt(db_session, tenant_id, co, ApplicationStatus.SUBMITTED, submitted_days_ago=1)
    elif prior == "in-flight-self":
        fake_attempt(db_session, tenant_id, co, ApplicationStatus.QUALIFIED, submitted_days_ago=0)
    elif prior == "released-self":
        fake_attempt(db_session, tenant_id, co, ApplicationStatus.CLOSED, submitted_days_ago=None, released=True)
    elif prior == "skipped":
        OpportunityRepository(db_session, tenant_id).transition(co, OpportunityState.SKIPPED, "user", "no")
        db_session.commit()
    elif prior == "closed":
        co.opportunity.status = "CLOSED"
        db_session.commit()

    decision = decisions_by_co(scheduler.preview())[co.id]
    assert decision.code is expected, decision.reason
    run = scheduler.run()
    db_session.refresh(co)
    assert co.scheduler_code == expected.value
    assert (run.admitted == 1) == (expected is AdmissionReason.ADMITTED)


def test_priority_never_decides_whether(db_session, tenant_id, scheduler, opportunities):
    """Same policy, two opportunities differing only in priority: identical verdicts."""
    a = opportunities.make(title="A", fit_score=50, company="Alpha")
    b = opportunities.make(title="B", fit_score=50, company="Beta")
    a.priority_score, b.priority_score = 100, 0
    db_session.commit()
    preview = scheduler.preview()
    by = decisions_by_co(preview)
    assert by[a.id].code is by[b.id].code is AdmissionReason.ADMITTED
    assert [d.candidate_opportunity_id for d in preview.decisions] == [a.id, b.id]
