"""Selection quality at admission: cool-down, duplicates, release and determinism still hold (2026-09-14)."""

from app.application.database.models import ApplicationRow
from app.application.models import ApplicationStatus
from app.execution.service import ExecutionService
from app.models.preference import Preference
from app.pipeline.database.models import ApplicationQueueRow
from app.pipeline.models import AdmissionReason
from tests.scheduler.conftest import decisions_by_co, fake_attempt

HYDERABAD = "Hyderabad, Telangana, India"


def _targets(evidence, **extra) -> None:
    evidence.upsert_profile({}, Preference(target_roles_tier1=["Software Engineer", "Backend Engineer"], **extra).model_dump(), actor="test")
    evidence.commit()


def test_company_cooldown_still_applies_between_two_relevant_roles(db_session, tenant_id, evidence, scheduler, opportunities):
    _targets(evidence)
    first = opportunities.make(title="Backend Engineer", fit_score=80, company="CoolCo", location=HYDERABAD, remote_type="UNKNOWN")
    second = opportunities.make(title="Software Engineer", fit_score=70, company="CoolCo", location=HYDERABAD, remote_type="UNKNOWN")
    run = scheduler.run()
    db_session.refresh(first)
    db_session.refresh(second)
    assert run.admitted == 1
    assert {first.scheduler_code, second.scheduler_code} == {AdmissionReason.ADMITTED.value, AdmissionReason.COOLDOWN_ACTIVE.value}


def test_no_duplicate_application_to_the_same_title_at_the_same_company(db_session, tenant_id, evidence, scheduler, opportunities):
    _targets(evidence)
    applied = opportunities.make(title="Backend Engineer", fit_score=80, company="DupCo", location=HYDERABAD, remote_type="UNKNOWN")
    fake_attempt(db_session, tenant_id, applied, ApplicationStatus.SUBMITTED, submitted_days_ago=1)
    again = opportunities.make(title="Backend Engineer", fit_score=80, company="DupCo", location="Secunderabad", remote_type="UNKNOWN")
    assert decisions_by_co(scheduler.preview())[again.id].code is AdmissionReason.DUPLICATE_APPLICATION


def test_cancelling_an_inappropriate_attempt_releases_the_slot_without_corrupting_the_queue(db_session, tenant_id, evidence, scheduler, opportunities):
    # Admitted before the selection fix: the candidate had opted into unrelated roles.
    _targets(evidence, role_include_unrelated=True)
    content = opportunities.make(title="Proprietary Content Creator", fit_score=40, company="ReleaseCo", location=HYDERABAD, remote_type="UNKNOWN")
    assert scheduler.run().admitted == 1
    attempt = db_session.query(ApplicationRow).filter(ApplicationRow.tenant_id == tenant_id, ApplicationRow.candidate_opportunity_id == content.id).one()
    engineering = opportunities.make(title="Software Engineer", fit_score=60, company="ReleaseCo", location=HYDERABAD, remote_type="UNKNOWN")
    assert decisions_by_co(scheduler.preview())[engineering.id].code is AdmissionReason.COOLDOWN_ACTIVE, "the unrelated attempt holds the company slot"

    _targets(evidence)
    ExecutionService(db_session, tenant_id, actor="test", executors={}).cancel(attempt.id, "test", "released: unrelated role family under the selection policy")
    db_session.refresh(attempt)
    assert attempt.status == ApplicationStatus.CANCELLED.value and attempt.released_at is not None
    item = db_session.query(ApplicationQueueRow).filter(ApplicationQueueRow.tenant_id == tenant_id, ApplicationQueueRow.opportunity_id == content.opportunity_id).one()
    assert item.state == "CANCELLED" and item.claimed_by is None

    assert scheduler.run().admitted == 1
    db_session.refresh(engineering)
    db_session.refresh(content)
    assert engineering.scheduler_code == AdmissionReason.ADMITTED.value
    assert content.scheduler_code == AdmissionReason.IRRELEVANT_ROLE.value
    assert scheduler.run().admitted == 0, "a second pass admits nothing new"
    items = db_session.query(ApplicationQueueRow).filter(ApplicationQueueRow.tenant_id == tenant_id, ApplicationQueueRow.opportunity_id == engineering.opportunity_id).all()
    assert len(items) == 1


def test_admission_order_is_deterministic(db_session, tenant_id, evidence, scheduler, opportunities):
    _targets(evidence)
    for index, (title, fit) in enumerate([("Backend Engineer", 55), ("Implementation Consultant", 80), ("Software Engineer", 75), ("Data Analyst", 60)]):
        opportunities.make(title=title, fit_score=fit, company=f"OrderCo{index}", location=HYDERABAD, remote_type="UNKNOWN")
    first = [d.candidate_opportunity_id for d in scheduler.preview().decisions]
    second = [d.candidate_opportunity_id for d in scheduler.preview().decisions]
    assert first == second
