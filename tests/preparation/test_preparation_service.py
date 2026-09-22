"""Preparation lifecycle: scope, idempotency, versions, review, isolation, prepared != submitted."""

import pytest

from app.application.database.models import ApplicationRow
from app.career.models import EvidenceNodeUpdate
from app.career.repository import EvidenceRepository
from app.core.errors import ConflictError, NotFoundError, PolicyBlocked
from app.models.enums import VerificationStatus
from app.pipeline.models import ApplicationPolicyUpdate, TailoringLevel
from app.pipeline.repository import OpportunityRepository, PolicyRepository
from app.preparation.models import PreparationStatus, PreparedAnswerStatus
from app.preparation.service import PreparationService


def test_preparation_belongs_to_the_candidate_opportunity(service, opportunities, db_session):
    co = opportunities.make(fit_score=60)
    prep = service.prepare(co.id)
    assert prep.candidate_opportunity_id == co.id and prep.opportunity_id == co.opportunity_id
    assert prep.tenant_id == service.tenant_id and prep.version == 1
    assert prep.job_id and prep.job_content_hash
    assert set(prep.inputs) >= {"evidence_fingerprint", "job_content_hash", "match_id", "tailoring_level", "policy_version", "template_version", "questions"}
    assert prep.status == PreparationStatus.READY.value
    assert db_session.query(ApplicationRow).filter_by(opportunity_id=co.opportunity_id).count() == 0, "prepared is not submitted"
    db_session.refresh(co)
    assert co.state == "PREPARED"
    events = service.repo.list_audit("preparation", prep.id)
    assert {e.action for e in events} == {"created", "validation_passed"}
    assert events[-1].after["positioning_reason"] and events[-1].after["evidence_selected"] > 0


def test_idempotent_then_new_version_on_changed_inputs(service, opportunities, db_session, tenant_id):
    co = opportunities.make(fit_score=60)
    first = service.prepare(co.id)
    assert service.prepare(co.id).id == first.id, "unchanged inputs reuse the package"
    report = service.prepare_many([co.id])
    assert report.reused == 1 and report.created == 0

    # Evidence changed -> new version, old superseded.
    EvidenceRepository(db_session, tenant_id).update_node("skill-python", EvidenceNodeUpdate(claim="Python (production services)"), actor="candidate")
    db_session.commit()
    service.context(refresh=True)
    second = service.prepare(co.id)
    assert second.version == 2 and second.id != first.id
    db_session.refresh(first)
    assert first.status == PreparationStatus.SUPERSEDED.value
    assert [e.action for e in service.repo.list_audit("preparation", first.id)][0] == "superseded"

    # Policy changed the level -> new version again.
    PolicyRepository(db_session, tenant_id).update(ApplicationPolicyUpdate(tailoring_by_band={"HIGH": TailoringLevel.L2, "MEDIUM": TailoringLevel.L2, "LOW": TailoringLevel.L0}), "test")
    db_session.commit()
    service.context(refresh=True)
    third = service.prepare(co.id)
    assert third.version == 3 and third.tailoring_level == "L2"
    assert [p.version for p in service.list_for(co.id)] == [3, 2, 1]


def test_user_input_flow_reaches_ready_and_can_save_to_bank(db_session, tenant_id, evidence, opportunities):
    service = PreparationService(db_session, tenant_id, actor="test")  # no answer bank
    co = opportunities.make(fit_score=60)
    prep = service.prepare(co.id)
    assert prep.status == PreparationStatus.NEEDS_USER_INPUT.value
    db_session.refresh(co)
    assert co.state == "IN_REVIEW"
    missing = [a for a in prep.answers if a.status == PreparedAnswerStatus.NEEDS_USER_INPUT.value]
    assert {a.category for a in missing} == {"work_authorization", "sponsorship", "notice_period", "salary"}
    assert {e.action for e in service.repo.list_audit("preparation", prep.id)} >= {"user_input_required"}
    with pytest.raises(ConflictError):
        service.approve(prep.id, "candidate")

    for answer in missing:
        service.answer_question(prep.id, answer.id, f"Answer for {answer.category}", "candidate", save_to_bank=True)
    db_session.commit()
    db_session.refresh(prep)
    assert prep.status == PreparationStatus.READY.value
    db_session.refresh(co)
    assert co.state == "PREPARED"
    assert evidence.find_answer("What is your notice period?").answer == "Answer for notice_period"

    # Next opportunity reuses the saved answers straight away.
    service2 = PreparationService(db_session, tenant_id, actor="test")
    other = opportunities.make(title="Data Engineer", fit_score=60)
    assert service2.prepare(other.id).status == PreparationStatus.READY.value


def test_review_and_invalidate(service, opportunities):
    co = opportunities.make(fit_score=60)
    prep = service.prepare(co.id)
    service.reject(prep.id, "candidate", "tone")
    assert prep.status == PreparationStatus.NEEDS_REVIEW.value
    service.approve(prep.id, "candidate")
    assert prep.status == PreparationStatus.READY.value and prep.approved_by == "candidate"
    service.invalidate(prep.id, "candidate", "evidence changed")
    assert prep.status == PreparationStatus.INVALIDATED.value
    with pytest.raises(ConflictError):
        service.approve(prep.id, "candidate")
    assert service.prepare(co.id).version == 2, "an invalidated package is rebuilt"


def test_not_admitted_and_ineligible_are_refused_unless_forced(service, opportunities):
    not_admitted = opportunities.make(fit_score=30, admitted=False)
    with pytest.raises(PolicyBlocked):
        service.prepare(not_admitted.id)
    assert service.prepare(not_admitted.id, force=True).status == PreparationStatus.READY.value
    ineligible = opportunities.make(title="Staff Engineer", fit_score=95, eligibility="INELIGIBLE")
    with pytest.raises(PolicyBlocked):
        service.prepare(ineligible.id, force=True)
    with pytest.raises(NotFoundError):
        service.prepare("does-not-exist")


def test_tenant_isolation(db_session, tenant_id, other_tenant_id, service, opportunities):
    co = opportunities.make(fit_score=60)
    prep = service.prepare(co.id)
    other = PreparationService(db_session, other_tenant_id, actor="other")
    assert other.get(prep.id) is None and other.latest(co.id) is None
    with pytest.raises(NotFoundError):
        other.require(prep.id)
    with pytest.raises(NotFoundError):
        other.prepare(co.id)  # candidate opportunity is not in that tenant
    assert other.list_all()[1] == 0
    # The other tenant's evidence snapshot is empty: nothing of tenant A leaks into it.
    assert other.context().snapshot.nodes == {} and other.context().snapshot.answers == []
    assert OpportunityRepository(db_session, other_tenant_id).list_audit("preparation", prep.id) == []


def test_removed_and_unverified_evidence_never_enters_a_package(db_session, tenant_id, service, opportunities):
    repo = EvidenceRepository(db_session, tenant_id)
    repo.remove_node("skill-redis", actor="candidate", reason="rusty")
    repo.update_node("skill-go", EvidenceNodeUpdate(verification_status=VerificationStatus.NEEDS_REVIEW), actor="candidate")
    db_session.commit()
    service.context(refresh=True)
    co = opportunities.make(fit_score=60, requirements=[("Redis", ["skill-redis"], 30.0), ("Go", ["skill-go"], 20.0), ("Python", ["skill-python"], 10.0)])
    prep = service.prepare(co.id)
    assert "skill-redis" not in prep.evidence_keys and "skill-go" not in prep.evidence_keys
    resume = next(a for a in prep.artifacts if a.artifact_type == "RESUME")
    skills = next(b for b in resume.blocks if b["section"] == "skills")["text"]
    assert "Redis" not in skills and "Go" not in skills.split(":")[1] and "Python" in skills
    created = service.repo.list_audit("preparation", prep.id)[-1]
    assert created.after["evidence_excluded"]["skill-redis"] == "removed"
    assert created.after["evidence_excluded"]["skill-go"].startswith("not application-safe")


def test_a_package_prepared_with_the_forms_own_questions_keeps_them_when_the_worker_prepares_again(db_session, tenant_id, evidence, opportunities):
    # First real dry run (Notion, Ashby): the form asks no salary / notice period / authorization.
    # The queue worker (questions=None) rebuilt the package with the standard set and blocked it on facts
    # the employer never asks for.
    service = PreparationService(db_session, tenant_id, actor="test")  # no answer bank
    co = opportunities.make(fit_score=60)
    form_questions = ["Why are you interested in this role?"]
    prep = service.prepare(co.id, questions=form_questions, force=True)
    assert prep.status == PreparationStatus.READY.value and [a.question for a in prep.answers] == form_questions

    assert service.prepare(co.id).id == prep.id, "the worker's call reuses the form-specific package"
    rebuilt = service.prepare(co.id, force=True)
    assert rebuilt.id != prep.id and [a.question for a in rebuilt.answers] == form_questions
    assert rebuilt.status == PreparationStatus.READY.value

    other = opportunities.make(title="Data Engineer", fit_score=60)
    standard = service.prepare(other.id)
    assert standard.status == PreparationStatus.NEEDS_USER_INPUT.value, "without the form's questions the standard set still applies"
