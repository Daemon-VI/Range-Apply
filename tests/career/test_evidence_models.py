"""EvidenceNode domain model: validation and the status -> grade mapping."""

import pytest
from pydantic import ValidationError

from app.career.models import (
    EvidenceGrade,
    EvidenceKind,
    EvidenceNode,
    EvidenceNodeCreate,
    EvidenceStatus,
    ImportReport,
    grade_for,
)
from app.models.enums import VerificationStatus


@pytest.mark.parametrize(
    "status, removed, expected",
    [
        (VerificationStatus.VERIFIED, False, EvidenceGrade.CONFIRMED),
        (VerificationStatus.INFERRED, False, EvidenceGrade.APPROXIMATE),
        (VerificationStatus.UNVERIFIED, False, EvidenceGrade.UNVERIFIED),
        (VerificationStatus.NEEDS_REVIEW, False, EvidenceGrade.NEEDS_REVIEW),
        (VerificationStatus.CONFLICT, False, EvidenceGrade.NEEDS_REVIEW),
        (VerificationStatus.VERIFIED, True, EvidenceGrade.REMOVED),
    ],
)
def test_grade_mapping(status, removed, expected):
    assert grade_for(status, removed) is expected


def test_only_confirmed_is_usable_in_application():
    assert EvidenceGrade.CONFIRMED.usable_in_application
    for grade in EvidenceGrade:
        if grade is not EvidenceGrade.CONFIRMED:
            assert not grade.usable_in_application


def test_node_grade_is_computed_and_serialised():
    node = EvidenceNode(
        id="n1",
        tenant_id="t",
        key="skill-x",
        kind=EvidenceKind.SKILL,
        label="X",
        claim="X",
        verification_status=VerificationStatus.VERIFIED,
    )
    assert node.grade is EvidenceGrade.CONFIRMED
    assert node.model_dump()["grade"] == "CONFIRMED"
    removed = node.model_copy(update={"status": EvidenceStatus.REMOVED})
    assert removed.grade is EvidenceGrade.REMOVED
    assert removed.is_removed


def test_node_validation_rejects_bad_confidence_and_empty_label():
    with pytest.raises(ValidationError):
        EvidenceNodeCreate(kind=EvidenceKind.SKILL, label="X", claim="X", confidence=1.5)
    with pytest.raises(ValidationError):
        EvidenceNodeCreate(kind=EvidenceKind.SKILL, label="", claim="X")


def test_import_report_counts():
    report = ImportReport(tenant_id="t", source="seed.json", created=["a"], skipped=["b", "c"])
    assert report.counts() == {"created": 1, "updated": 0, "skipped": 2, "relationships_created": 0}
    assert report.changed
    assert not ImportReport(tenant_id="t", source="s", skipped=["x"]).changed
