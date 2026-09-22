"""Audit regression (2026-09-14): the answer-bank category fallback is limited to candidate facts.

An approved "Why do you want to work at Acme?" entry used to answer BetaCorp's
"Why do you want to join our company?" through the category fallback.
"""

from types import SimpleNamespace

import pytest

from app.preparation.models import AnswerSource, PreparedAnswerStatus
from app.preparation.questions import AnswerResolver, QuestionCategory, classify_question


def entry(category, question, answer, entry_id):
    return SimpleNamespace(id=entry_id, category=category, question=question, question_key=classify_question(question).key, answer=answer, evidence_keys=[])


class Snapshot:
    def __init__(self, entries):
        self.answer_by_key = {e.question_key: e for e in entries}
        self.answers_by_category = {}
        for e in entries:
            self.answers_by_category.setdefault(e.category, []).append(e)
        self.profile = None
        self.preferences = None
        self.nodes = {}

    def of_kind(self, kind):
        return []

    def skill_by_label(self, label):
        return None

    def is_safe(self, key):
        return False


SELECTION = SimpleNamespace(skills=[], projects=[], matched_requirements={})


def resolve(entries, question, company="BetaCorp"):
    return AnswerResolver(Snapshot(entries)).resolve(question, SELECTION, "Backend Engineer", company)


def test_employer_specific_answer_is_not_reused_for_another_employer():
    acme = entry("why_company", "Why do you want to work at Acme?", "Acme's payments platform is where I want to grow.", "e1")
    assert classify_question("Why do you want to join our company?").category is QuestionCategory.WHY_COMPANY
    result = resolve([acme], "Why do you want to join our company?")
    assert result.source is not AnswerSource.ANSWER_BANK
    assert result.answer is None or "Acme" not in result.answer
    # the exact question still reuses its own approved answer
    same = resolve([acme], "Why do you want to work at Acme?", company="Acme")
    assert same.source is AnswerSource.ANSWER_BANK and same.entry_id == "e1"


def test_topic_specific_experience_answer_is_not_reused_for_another_topic():
    kafka = entry("experience_with", "Describe your experience with Kafka.", "Kafka: built event pipelines.", "k1")
    assert classify_question("Describe your experience with Redis.").category is QuestionCategory.EXPERIENCE_WITH
    result = resolve([kafka], "Describe your experience with Redis.")
    assert result.source is not AnswerSource.ANSWER_BANK
    assert result.answer is None or "Kafka" not in result.answer


def test_voluntary_answers_are_not_reused_by_category():
    stored = entry("voluntary", "What is your gender?", "Prefer not to say", "v1")
    result = resolve([stored], "Do you identify as a veteran?")
    assert result.source is not AnswerSource.ANSWER_BANK


@pytest.mark.parametrize(
    ("category", "stored_question", "asked"),
    [
        ("work_authorization", "Are you legally authorized to work in India?", "Do you have the right to work in India?"),
        ("notice_period", "What is your notice period?", "How soon can you join?"),
        ("salary", "What are your salary expectations?", "What is your expected CTC?"),
    ],
)
def test_candidate_facts_still_share_one_answer_across_wordings(category, stored_question, asked):
    stored = entry(category, stored_question, "stored fact", "f1")
    result = resolve([stored], asked)
    assert result.status is PreparedAnswerStatus.ANSWERED and result.source is AnswerSource.ANSWER_BANK and result.entry_id == "f1"
