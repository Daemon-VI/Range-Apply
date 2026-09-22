"""Answer bank reuse, profile facts, evidence-backed templates, explicit user input."""

import pytest

from app.career.models import AnswerBankEntryCreate, AnswerStatus
from app.pipeline.models import TailoringLevel
from app.preparation.evidence import EvidenceSnapshot, select_evidence
from app.preparation.models import AnswerSource, PreparedAnswerStatus
from app.preparation.questions import AnswerResolver, QuestionCategory, classify_question
from tests.preparation.test_evidence_snapshot import _assessment


@pytest.mark.parametrize(
    "question, category",
    [
        ("Are you legally authorized to work in India?", QuestionCategory.WORK_AUTHORIZATION),
        ("Do you have the right to work in the UK?", QuestionCategory.WORK_AUTHORIZATION),
        ("Will you now or in the future require visa sponsorship?", QuestionCategory.SPONSORSHIP),
        ("Are you willing to relocate to Bangalore?", QuestionCategory.RELOCATION),
        ("What is your notice period?", QuestionCategory.NOTICE_PERIOD),
        ("What are your salary expectations (CTC)?", QuestionCategory.SALARY),
        ("Why are you interested in this role?", QuestionCategory.WHY_ROLE),
        ("Why do you want to work at Acme?", QuestionCategory.WHY_COMPANY),
        ("Tell us about yourself.", QuestionCategory.ABOUT_YOU),
        ("Describe a relevant project.", QuestionCategory.RELEVANT_PROJECT),
        ("Describe your experience with Redis.", QuestionCategory.EXPERIENCE_WITH),
        ("Do you have a security clearance?", QuestionCategory.CLEARANCE),
        ("Gender (voluntary self-identification)", QuestionCategory.VOLUNTARY),
        ("What is your favourite colour?", QuestionCategory.OTHER),
    ],
)
def test_classification(question, category):
    assert classify_question(question).category is category


def _resolver(evidence):
    snapshot = EvidenceSnapshot.load(evidence)
    selection = select_evidence(snapshot, [_assessment("Go", ["skill-go"], 30.0), _assessment("Python", ["skill-python"], 20.0)], None, TailoringLevel.L1)
    return AnswerResolver(snapshot), selection


def test_equivalent_fact_questions_reuse_one_bank_entry_and_unrelated_do_not(answered_bank):
    resolver, selection = _resolver(answered_bank)
    a = resolver.resolve("Are you legally authorized to work in this country?", selection, "Backend Engineer", "Acme")
    b = resolver.resolve("Do you have the right to work in India?", selection, "Backend Engineer", "Acme")
    assert a.status is PreparedAnswerStatus.ANSWERED and a.source is AnswerSource.ANSWER_BANK
    assert a.entry_id == b.entry_id and b.reason == "approved answer bank entry"
    other = resolver.resolve("What is your favourite colour?", selection, "Backend Engineer", "Acme")
    assert other.status is PreparedAnswerStatus.NEEDS_REVIEW and other.entry_id is None


def test_missing_candidate_facts_become_user_input_not_guesses(evidence):
    resolver, selection = _resolver(evidence)
    for question in ("What is your notice period?", "Are you willing to relocate?", "What are your salary expectations?", "Do you hold a security clearance?"):
        resolved = resolver.resolve(question, selection, "Backend Engineer", "Acme")
        assert resolved.status is PreparedAnswerStatus.NEEDS_USER_INPUT and resolved.answer is None, question


def test_profile_and_evidence_answers(evidence):
    from app.career.repository import EvidenceRepository  # noqa: F401 - type hint only

    evidence.upsert_profile({"work_authorization": "Indian citizen, no sponsorship needed"}, None, actor="test")
    evidence.commit()
    resolver, selection = _resolver(evidence)
    auth = resolver.resolve("Are you authorized to work in India?", selection, "Backend Engineer", "Acme")
    assert auth.source is AnswerSource.PROFILE and auth.answer.startswith("Indian citizen")
    education = resolver.resolve("What is your highest degree?", selection, "Backend Engineer", "Acme")
    assert education.status is PreparedAnswerStatus.ANSWERED and education.evidence_keys == ["education:primary"]

    why = resolver.resolve("Why are you interested in this role?", selection, "Backend Engineer", "Acme")
    assert why.source is AnswerSource.GENERATED and "Backend Engineer" in why.answer and "Acme" in why.answer
    assert "skill-go" in why.evidence_keys and "Go" in why.answer

    redis = resolver.resolve("Describe your experience with Redis.", selection, "Backend Engineer", "Acme")
    assert redis.status is PreparedAnswerStatus.ANSWERED and "skill-redis" in redis.evidence_keys and "Ticket Engine" in redis.answer
    kube = resolver.resolve("Describe your experience with Kubernetes.", selection, "Backend Engineer", "Acme")
    assert kube.status is PreparedAnswerStatus.NEEDS_USER_INPUT, "no confirmed evidence: ask, do not invent"
    java = resolver.resolve("Describe your experience with Java.", selection, "Backend Engineer", "Acme")
    assert java.status is PreparedAnswerStatus.NEEDS_USER_INPUT, "UNVERIFIED skill cannot become a claim"

    project = resolver.resolve("Describe a relevant project.", selection, "Backend Engineer", "Acme")
    assert project.status is PreparedAnswerStatus.ANSWERED and project.evidence_keys == ["ticket-engine"]


def test_category_reuse_for_narrative_questions(evidence):
    """Audit (2026-09-14): generic narratives are shared across wordings; employer-specific ones are not.

    A "why this role" answer was saved while applying to one employer; reusing it
    by category for every other employer's "why" question is the leak the audit fixed.
    """
    evidence.create_answer(
        AnswerBankEntryCreate(category="why_role", question="Why this position?", answer="Because backend systems are my focus.", status=AnswerStatus.APPROVED),
        actor="test",
    )
    evidence.create_answer(
        AnswerBankEntryCreate(category="about_you", question="Tell us about yourself.", answer="I build backend systems.", status=AnswerStatus.APPROVED),
        actor="test",
    )
    evidence.commit()
    resolver, selection = _resolver(evidence)
    why = resolver.resolve("Why are you interested in this role?", selection, "Backend Engineer", "Acme")
    assert why.source is not AnswerSource.ANSWER_BANK, "an employer-specific answer is not reused by category"
    about = resolver.resolve("Please introduce yourself.", selection, "Backend Engineer", "Acme")
    assert about.source is AnswerSource.ANSWER_BANK and about.answer == "I build backend systems."


def test_employer_specific_answers_are_reused_by_exact_question_only_for_the_same_employer(evidence):
    """Backend audit (2026-09-14): generic wording is shared by many employers' forms, so a saved
    "Why do you want to work here?" answer was reused verbatim, by exact question, for every other company."""
    evidence.create_answer(
        AnswerBankEntryCreate(category="why_company", question="Why do you want to work here?", answer="Notion's tools shaped how I study.", status=AnswerStatus.APPROVED),
        actor="test",
    )
    evidence.create_answer(
        AnswerBankEntryCreate(category="why_company", question="Why do you want to work at Acme?", answer="Acme's ticketing problems match my project.", status=AnswerStatus.APPROVED),
        actor="test",
    )
    evidence.commit()
    resolver, selection = _resolver(evidence)
    generic = resolver.resolve("Why do you want to work here?", selection, "Backend Engineer", "Acme")
    assert generic.source is not AnswerSource.ANSWER_BANK and "Notion" not in (generic.answer or "")
    named = resolver.resolve("Why do you want to work at Acme?", selection, "Backend Engineer", "Acme")
    assert named.source is AnswerSource.ANSWER_BANK and named.answer.startswith("Acme's")
    other_employer = resolver.resolve("Why do you want to work at Acme?", selection, "Backend Engineer", "Globex")
    assert other_employer.source is not AnswerSource.ANSWER_BANK
