"""Answer bank: candidate-approved answers are reused deterministically."""

import pytest

from app.career.models import AnswerBankEntryCreate, AnswerBankEntryUpdate, AnswerStatus
from app.career.repository import normalize_question
from app.core.errors import ConflictError, ValidationFailed


def _entry(**kwargs) -> AnswerBankEntryCreate:
    return AnswerBankEntryCreate(
        category=kwargs.pop("category", "work_authorization"),
        question=kwargs.pop("question", "Are you legally authorized to work in India?"),
        answer=kwargs.pop("answer", "Yes, I am an Indian citizen and require no sponsorship."),
        **kwargs,
    )


def test_normalize_question_is_stable():
    assert normalize_question("Are you AUTHORIZED to work in India?") == normalize_question(
        "are you authorized to work in india"
    )
    assert normalize_question("  Notice   period? ") == "notice period"


def test_draft_is_not_reused_until_approved(imported_repo):
    repo = imported_repo
    row = repo.create_answer(_entry(evidence_keys=["fact-name"]), actor="test")
    repo.commit()
    assert row.status == AnswerStatus.DRAFT.value
    assert repo.find_answer("are you authorized to work in india") is None

    approved = repo.approve_answer(row.id, actor="candidate")
    assert approved.status == AnswerStatus.APPROVED.value
    assert approved.approved_at is not None
    assert approved.version == 2
    found = repo.find_answer("ARE YOU legally authorized to work in India")
    assert found is not None and found.id == row.id
    assert repo.find_answer("Do you need sponsorship?") is None  # no fuzzy matching

    events = repo.list_audit("answer_bank_entry", row.id)
    assert [e.action for e in events] == ["updated", "created"]
    assert events[0].before["status"] == "DRAFT" and events[0].after["status"] == "APPROVED"


def test_editing_an_approved_answer_returns_it_to_draft(imported_repo):
    repo = imported_repo
    row = repo.create_answer(_entry(status=AnswerStatus.APPROVED), actor="test")
    assert repo.find_answer(row.question) is not None
    edited = repo.update_answer(row.id, AnswerBankEntryUpdate(answer="Yes."), actor="api")
    assert edited.status == AnswerStatus.DRAFT.value and edited.approved_at is None
    assert repo.find_answer(row.question) is None


def test_duplicate_question_and_unknown_evidence_are_rejected(imported_repo):
    repo = imported_repo
    repo.create_answer(_entry(), actor="test")
    with pytest.raises(ConflictError):
        repo.create_answer(_entry(question="are you legally authorized to work in india"), actor="t")
    with pytest.raises(ValidationFailed):
        repo.create_answer(_entry(question="Notice period?", evidence_keys=["nope"]), actor="t")


def test_answers_are_tenant_scoped(imported_repo, other_repo):
    row = imported_repo.create_answer(_entry(status=AnswerStatus.APPROVED), actor="t")
    imported_repo.commit()
    assert other_repo.find_answer(row.question) is None
    assert other_repo.get_answer(row.id) is None
    assert other_repo.list_answers() == []
    # Same question may be answered differently by another tenant.
    other = other_repo.create_answer(_entry(answer="No."), actor="t")
    assert other.id != row.id


def test_list_filters(imported_repo):
    repo = imported_repo
    repo.create_answer(_entry(), actor="t")
    repo.create_answer(_entry(category="relocation", question="Willing to relocate?", answer="Yes, within India.", status=AnswerStatus.APPROVED), actor="t")
    assert len(repo.list_answers()) == 2
    assert [a.category for a in repo.list_answers(category="relocation")] == ["relocation"]
    assert len(repo.list_answers(status=AnswerStatus.APPROVED)) == 1
