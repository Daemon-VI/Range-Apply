"""Positioning variants select, order and frame existing evidence only."""

import pytest

from app.career.models import (
    PositioningVariantCreate,
    PositioningVariantEvidence,
    PositioningVariantUpdate,
)
from app.core.errors import ConflictError, NotFoundError, ValidationFailed


def _create(repo, keys, **kwargs):
    return repo.create_variant(
        PositioningVariantCreate(
            role_family=kwargs.pop("role_family", "Backend Engineer"),
            headline=kwargs.pop("headline", "Backend-leaning engineering student"),
            summary=kwargs.pop("summary", "Go, Redis, MySQL, load-tested systems."),
            evidence=[PositioningVariantEvidence(key=k, position=i) for i, k in enumerate(keys)],
            **kwargs,
        ),
        actor="test",
    )


def test_create_with_ordered_evidence(imported_repo):
    repo = imported_repo
    row = _create(repo, ["ticket-engine", "skill-go", "skill-redis"])
    repo.commit()
    snapshot = repo.variant_snapshot(row)
    assert [e["key"] for e in snapshot["evidence"]] == ["ticket-engine", "skill-go", "skill-redis"]
    assert row.name == "Backend Engineer" and row.is_active and row.version == 1
    assert repo.list_variants(role_family="Backend Engineer", active_only=True)[0].id == row.id
    assert repo.list_audit("positioning_variant", row.id)[0].action == "created"


def test_unknown_or_removed_evidence_is_rejected(imported_repo):
    repo = imported_repo
    with pytest.raises(ValidationFailed) as exc:
        _create(repo, ["ticket-engine", "skill-does-not-exist"])
    assert "skill-does-not-exist" in exc.value.details["missing"]

    repo.remove_node("skill-java", actor="test")
    with pytest.raises(ValidationFailed):
        _create(repo, ["skill-java"])

    with pytest.raises(ValidationFailed):
        _create(repo, ["skill-go", "skill-go"])


def test_duplicate_name_per_role_family_is_a_conflict(imported_repo):
    _create(imported_repo, ["skill-go"])
    with pytest.raises(ConflictError):
        _create(imported_repo, ["skill-redis"])
    _create(imported_repo, ["skill-redis"], name="alt")  # different name is fine


def test_update_reorders_evidence_bumps_version_and_audits(imported_repo):
    repo = imported_repo
    row = _create(repo, ["skill-go", "skill-redis"])
    updated = repo.update_variant(
        row.id,
        PositioningVariantUpdate(
            headline="Systems-minded backend engineer",
            evidence=[
                PositioningVariantEvidence(key="skill-redis", position=0),
                PositioningVariantEvidence(key="skill-go", position=1),
                PositioningVariantEvidence(key="ticket-engine", position=2, section="projects"),
            ],
        ),
        actor="api",
    )
    assert updated.version == 2
    snapshot = repo.variant_snapshot(updated)
    assert [e["key"] for e in snapshot["evidence"]] == ["skill-redis", "skill-go", "ticket-engine"]
    assert snapshot["evidence"][2]["section"] == "projects"
    event = repo.list_audit("positioning_variant", row.id)[0]
    assert event.action == "updated"
    assert [e["key"] for e in event.before["evidence"]] == ["skill-go", "skill-redis"]

    same = repo.update_variant(row.id, PositioningVariantUpdate(headline="Systems-minded backend engineer"), actor="api")
    assert same.version == 2


def test_delete_and_tenant_isolation(imported_repo, other_repo):
    repo = imported_repo
    row = _create(repo, ["skill-go"])
    repo.commit()
    assert other_repo.get_variant(row.id) is None
    with pytest.raises(NotFoundError):
        other_repo.delete_variant(row.id, actor="other")
    repo.delete_variant(row.id, actor="api")
    assert repo.get_variant(row.id) is None
    assert repo.list_audit("positioning_variant", row.id)[0].action == "deleted"


def test_removing_evidence_node_drops_it_from_variants(imported_repo, db_session):
    repo = imported_repo
    row = _create(repo, ["skill-go", "skill-redis"])
    repo.commit()
    node = repo.require_node("skill-redis")
    db_session.delete(node)  # hard delete cascades; soft removal keeps history
    db_session.commit()
    db_session.refresh(row)
    assert [e.node.key for e in row.evidence] == ["skill-go"]
