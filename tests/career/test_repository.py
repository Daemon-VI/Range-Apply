"""EvidenceRepository: persistence round trip, provenance, grading rules,
tenant isolation, relationships and audit/versioning."""

import pytest

from app.career.models import (
    EvidenceGrade,
    EvidenceKind,
    EvidenceNode,
    EvidenceNodeCreate,
    EvidenceNodeUpdate,
    EvidenceSourceType,
    RelationType,
)
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.models.enums import VerificationStatus


def _skill(key="skill-rust", status=VerificationStatus.VERIFIED, **kwargs) -> EvidenceNodeCreate:
    return EvidenceNodeCreate(
        key=key,
        kind=EvidenceKind.SKILL,
        label=kwargs.pop("label", "Rust"),
        claim=kwargs.pop("claim", "Rust (used in a systems project)"),
        attributes={"category": "Programming", "evidence": "systems project"},
        verification_status=status,
        source_type=kwargs.pop("source_type", EvidenceSourceType.CANDIDATE_ENTERED),
        source_ref=kwargs.pop("source_ref", "manual entry"),
        **kwargs,
    )


def test_create_and_round_trip(repo):
    row = repo.create_node(_skill(), actor="test")
    repo.commit()

    fetched = repo.require_node("skill-rust")
    node = EvidenceNode.model_validate(fetched)
    assert node.tenant_id == repo.tenant_id
    assert node.kind is EvidenceKind.SKILL
    assert node.attributes == {"category": "Programming", "evidence": "systems project"}
    assert node.grade is EvidenceGrade.CONFIRMED
    assert node.version == 1
    assert node.created_at is not None and node.updated_at is not None
    assert row.id == fetched.id


def test_provenance_is_recorded(repo):
    repo.create_node(
        _skill(source_type=EvidenceSourceType.RESUME, source_ref="resume-2026.pdf#p1"),
        actor="test",
    )
    node = EvidenceNode.model_validate(repo.require_node("skill-rust"))
    assert node.source_type is EvidenceSourceType.RESUME
    assert node.source_ref == "resume-2026.pdf#p1"
    # A VERIFIED creation records who vouched for it.
    assert node.verified_by == "test"
    assert node.verified_at is not None


def test_verified_defaults_allowed_flags_and_unverified_does_not(repo):
    verified = repo.create_node(_skill("a"), actor="t")
    unverified = repo.create_node(_skill("b", status=VerificationStatus.UNVERIFIED), actor="t")
    assert verified.allowed_for_resume and verified.allowed_for_application
    assert not unverified.allowed_for_resume and not unverified.allowed_for_application


def test_duplicate_key_is_a_conflict(repo):
    repo.create_node(_skill(), actor="t")
    with pytest.raises(ConflictError):
        repo.create_node(_skill(), actor="t")


def test_derived_key_when_omitted(repo):
    row = repo.create_node(
        EvidenceNodeCreate(kind=EvidenceKind.SKILL, label="Apache Kafka", claim="Kafka"), actor="t"
    )
    assert row.key == "skill:apache-kafka"


@pytest.mark.parametrize(
    "source_type", [EvidenceSourceType.LLM_EXTRACTED, EvidenceSourceType.INFERRED]
)
def test_extraction_is_not_verification(repo, source_type):
    with pytest.raises(ValidationFailed) as exc:
        repo.create_node(_skill(source_type=source_type), actor="t")
    assert "Extraction is not verification" in str(exc.value)

    # Created as NEEDS_REVIEW instead, then explicitly confirmed by a human.
    repo.create_node(
        _skill(status=VerificationStatus.NEEDS_REVIEW, source_type=source_type), actor="t"
    )
    row = repo.update_node(
        "skill-rust",
        EvidenceNodeUpdate(verification_status=VerificationStatus.VERIFIED),
        actor="candidate",
    )
    assert row.verified_by == "candidate"
    assert EvidenceNode.model_validate(row).grade is EvidenceGrade.CONFIRMED
    # Origin provenance survives confirmation.
    assert row.source_type == source_type.value


@pytest.mark.parametrize(
    "status, resume, application",
    [
        (VerificationStatus.UNVERIFIED, False, True),
        (VerificationStatus.NEEDS_REVIEW, False, True),
        (VerificationStatus.INFERRED, True, False),
        (VerificationStatus.CONFLICT, True, False),
    ],
)
def test_truth_rules_reject_overstated_flags(repo, status, resume, application):
    with pytest.raises(ValidationFailed):
        repo.create_node(
            _skill(status=status, allowed_for_resume=resume, allowed_for_application=application),
            actor="t",
        )


def test_update_bumps_version_and_records_before_after(repo):
    repo.create_node(_skill(), actor="importer")
    repo.commit()
    row = repo.update_node("skill-rust", EvidenceNodeUpdate(label="Rust (2021 edition)"), actor="api")
    repo.commit()
    assert row.version == 2
    assert row.label == "Rust (2021 edition)"

    events = repo.list_audit("evidence_node", "skill-rust")
    assert [e.action for e in events] == ["updated", "created"]
    update = events[0]
    assert update.actor == "api"
    assert update.tenant_id == repo.tenant_id
    assert update.before["label"] == "Rust"
    assert update.after["label"] == "Rust (2021 edition)"
    assert update.before["version"] == 1 and update.after["version"] == 2


def test_noop_update_does_not_bump_or_audit(repo):
    repo.create_node(_skill(), actor="t")
    row = repo.update_node("skill-rust", EvidenceNodeUpdate(label="Rust"), actor="api")
    assert row.version == 1
    assert [e.action for e in repo.list_audit("evidence_node", "skill-rust")] == ["created"]


def test_downgrading_status_clears_allowed_flags_and_verifier(repo):
    repo.create_node(_skill(), actor="t")
    row = repo.update_node(
        "skill-rust",
        EvidenceNodeUpdate(verification_status=VerificationStatus.UNVERIFIED),
        actor="api",
    )
    assert not row.allowed_for_application and not row.allowed_for_resume
    assert row.verified_by is None and row.verified_at is None
    assert EvidenceNode.model_validate(row).grade is EvidenceGrade.UNVERIFIED


def test_remove_and_restore(repo):
    repo.create_node(_skill(), actor="t")
    removed = repo.remove_node("skill-rust", actor="api", reason="not true anymore")
    assert EvidenceNode.model_validate(removed).grade is EvidenceGrade.REMOVED
    assert removed.removed_at is not None
    assert [n.key for n in repo.list_nodes()] == []
    assert [n.key for n in repo.list_nodes(include_removed=True)] == ["skill-rust"]
    assert repo.get_node("skill-rust", include_removed=False) is None

    events = repo.list_audit("evidence_node", "skill-rust")
    assert events[0].action == "removed" and events[0].summary == "not true anymore"

    restored = repo.restore_node("skill-rust", actor="api")
    assert restored.status == "ACTIVE" and restored.removed_at is None
    assert restored.version == 3
    assert [n.key for n in repo.list_nodes()] == ["skill-rust"]


def test_missing_node_raises_not_found(repo):
    with pytest.raises(NotFoundError):
        repo.require_node("nope")
    with pytest.raises(NotFoundError):
        repo.update_node("nope", EvidenceNodeUpdate(label="x"), actor="t")


# ---------------------------------------------------------------------- #
# tenant isolation
# ---------------------------------------------------------------------- #


def test_tenants_cannot_see_each_others_evidence(repo, other_repo):
    repo.create_node(_skill(), actor="t")
    other_repo.create_node(_skill(label="Rust", claim="Rust elsewhere"), actor="t")
    repo.commit()

    assert repo.require_node("skill-rust").claim == "Rust (used in a systems project)"
    assert other_repo.require_node("skill-rust").claim == "Rust elsewhere"
    assert repo.require_node("skill-rust").id != other_repo.require_node("skill-rust").id

    repo.create_node(_skill(key="only-mine"), actor="t")
    assert other_repo.get_node("only-mine") is None
    assert {n.key for n in other_repo.list_nodes()} == {"skill-rust"}
    assert other_repo.list_audit(entity_id="only-mine") == []

    # A relationship cannot reach across tenants.
    with pytest.raises(NotFoundError):
        other_repo.add_relationship("skill-rust", "only-mine", RelationType.SUPPORTS, actor="t")


def test_same_key_in_two_tenants_is_allowed(repo, other_repo):
    repo.create_node(_skill(key="dup"), actor="t")
    other_repo.create_node(_skill(key="dup"), actor="t")
    repo.commit()
    assert repo.require_node("dup").tenant_id == repo.tenant_id
    assert other_repo.require_node("dup").tenant_id == other_repo.tenant_id


# ---------------------------------------------------------------------- #
# relationships
# ---------------------------------------------------------------------- #


def test_relationships_are_idempotent_ordered_and_audited(repo):
    repo.create_node(
        EvidenceNodeCreate(key="proj", kind=EvidenceKind.PROJECT, label="P", claim="P",
                           verification_status=VerificationStatus.VERIFIED),
        actor="t",
    )
    repo.create_node(_skill("skill-a"), actor="t")
    repo.create_node(_skill("skill-b"), actor="t")

    _, created = repo.add_relationship("proj", "skill-b", RelationType.DEMONSTRATES, "t", position=1)
    assert created
    _, created_again = repo.add_relationship("proj", "skill-b", RelationType.DEMONSTRATES, "t", position=1)
    assert not created_again
    repo.add_relationship("proj", "skill-a", RelationType.DEMONSTRATES, "t", position=0)

    rels = repo.list_relationships("proj")
    assert [(r.to_node.key, r.relation) for r in rels] == [
        ("skill-a", "DEMONSTRATES"),
        ("skill-b", "DEMONSTRATES"),
    ]
    assert len(repo.list_relationships("skill-a")) == 1

    assert repo.remove_relationship("proj", "skill-a", RelationType.DEMONSTRATES, "t") is True
    assert repo.remove_relationship("proj", "skill-a", RelationType.DEMONSTRATES, "t") is False
    actions = [e.action for e in repo.list_audit("evidence_relationship")]
    assert actions.count("created") == 2 and actions.count("deleted") == 1


def test_removing_a_node_cascades_its_relationships(repo, db_session):
    repo.create_node(
        EvidenceNodeCreate(key="proj", kind=EvidenceKind.PROJECT, label="P", claim="P"), actor="t"
    )
    repo.create_node(_skill("skill-a"), actor="t")
    repo.add_relationship("proj", "skill-a", RelationType.DEMONSTRATES, "t")
    repo.commit()
    node = repo.require_node("skill-a")
    db_session.delete(node)
    db_session.commit()
    assert repo.list_relationships("proj") == []
