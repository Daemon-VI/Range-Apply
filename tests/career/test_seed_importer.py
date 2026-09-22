"""Seed import: correctness against data/career_seed.json, idempotency,
duplicate prevention, provenance, derived nodes and relationships."""

import copy

from app.career.importer import SeedImporter, record_hash
from app.career.models import EvidenceKind, EvidenceNode, EvidenceSourceType, RelationType
from app.models.enums import VerificationStatus


def test_import_creates_profile_and_every_seed_record(repo, seed_data, seed_path):
    report = SeedImporter(repo).import_file(seed_path)

    assert report.tenant_id == repo.tenant_id
    assert report.profile == "created"
    assert report.updated == [] and report.skipped == []

    profile = repo.get_profile_row()
    assert profile is not None
    assert profile.name == seed_data["profile"]["name"]
    assert profile.preferences["graduation_eligibility"] == 2027
    assert profile.version == 1

    keys = set(report.created)
    for collection in ("skills", "projects", "experience", "achievements", "facts"):
        for record in seed_data[collection]:
            assert record["id"] in keys, f"{collection}/{record['id']} not imported"

    by_kind = {}
    for node in repo.list_nodes():
        by_kind.setdefault(node.kind, 0)
        by_kind[node.kind] += 1
    assert by_kind[EvidenceKind.SKILL.value] == len(seed_data["skills"])
    assert by_kind[EvidenceKind.PROJECT.value] == len(seed_data["projects"])
    assert by_kind[EvidenceKind.EXPERIENCE.value] == len(seed_data["experience"])
    assert by_kind[EvidenceKind.ACHIEVEMENT.value] == len(seed_data["achievements"])
    assert by_kind[EvidenceKind.FACT.value] == len(seed_data["facts"])
    assert by_kind[EvidenceKind.METRIC.value] == sum(
        len(p.get("metrics") or []) for p in seed_data["projects"]
    )
    assert by_kind[EvidenceKind.RESPONSIBILITY.value] == sum(
        len(e.get("responsibilities") or []) for e in seed_data["experience"]
    )
    assert by_kind[EvidenceKind.EDUCATION.value] == 1


def test_import_preserves_provenance_and_status(imported_repo, seed_data, seed_path):
    python = EvidenceNode.model_validate(imported_repo.require_node("skill-python"))
    assert python.source_type is EvidenceSourceType.SEED_FILE
    assert python.source_ref == f"{seed_path.split('/')[-1].split(chr(92))[-1]}#skills/skill-python"
    assert python.verification_status is VerificationStatus.VERIFIED
    assert python.verified_by == "importer"
    seed_record = next(s for s in seed_data["skills"] if s["id"] == "skill-python")
    assert python.source_hash == record_hash(seed_record)

    java = EvidenceNode.model_validate(imported_repo.require_node("skill-java"))
    assert java.verification_status is VerificationStatus.UNVERIFIED  # never upgraded on import
    assert not java.allowed_for_application

    inferred = EvidenceNode.model_validate(imported_repo.require_node("fact-inferred-location"))
    assert inferred.source_type is EvidenceSourceType.INFERRED
    assert inferred.grade.value == "APPROXIMATE"
    assert inferred.confidence == 0.8

    throughput = EvidenceNode.model_validate(
        imported_repo.require_node("fact-ticket-engine-throughput")
    )
    assert throughput.verification_status is VerificationStatus.UNVERIFIED
    assert throughput.confidence == 0.7
    assert throughput.attributes["related_entity_id"] == "ticket-engine"


def test_derived_nodes_and_relationships(imported_repo, seed_data):
    repo = imported_repo
    ticket = repo.require_node("ticket-engine")
    rels = repo.list_relationships("ticket-engine")
    metrics = [r for r in rels if r.relation == RelationType.HAS_METRIC.value]
    seed_metrics = next(p for p in seed_data["projects"] if p["id"] == "ticket-engine")["metrics"]
    assert [r.to_node.label for r in sorted(metrics, key=lambda r: r.position)] == [
        m["name"] for m in seed_metrics
    ]
    assert all(r.to_node.verification_status == "UNVERIFIED" for r in metrics)
    assert all(r.from_node_id == ticket.id for r in metrics)

    demonstrates = [r for r in rels if r.relation == RelationType.DEMONSTRATES.value]
    assert {r.to_node.key for r in demonstrates} >= {"skill-go", "skill-redis", "skill-mysql"}

    about = [r for r in repo.list_relationships("fact-ticket-engine-arch")]
    assert [(r.relation, r.to_node.key) for r in about] == [("ABOUT", "ticket-engine")]

    exp = repo.list_relationships("exp-ticket-engine")
    responsibilities = [r for r in exp if r.relation == RelationType.HAS_RESPONSIBILITY.value]
    seed_exp = next(e for e in seed_data["experience"] if e["id"] == "exp-ticket-engine")
    assert [r.to_node.claim for r in sorted(responsibilities, key=lambda r: r.position)] == (
        seed_exp["responsibilities"]
    )

    education = EvidenceNode.model_validate(repo.require_node("education:primary"))
    assert education.kind is EvidenceKind.EDUCATION
    assert "MGIT" in education.claim


def test_reimport_is_idempotent(repo, seed_path):
    first = SeedImporter(repo).import_file(seed_path)
    audit_count = len(repo.list_audit(limit=10_000))
    versions = {n.key: n.version for n in repo.list_nodes()}

    second = SeedImporter(repo).import_file(seed_path)

    assert second.profile == "skipped"
    assert second.created == [] and second.updated == []
    assert set(second.skipped) == set(first.created)
    assert second.relationships_created == 0
    assert not second.changed
    assert len(repo.list_audit(limit=10_000)) == audit_count
    assert {n.key: n.version for n in repo.list_nodes()} == versions
    assert len(repo.list_nodes()) == len(first.created)


def test_changed_seed_record_updates_node_with_audit(repo, seed_data, seed_path):
    SeedImporter(repo).import_file(seed_path)
    changed = copy.deepcopy(seed_data)
    java = next(s for s in changed["skills"] if s["id"] == "skill-java")
    java["verification_status"] = "VERIFIED"
    java["evidence"] = "Confirmed by candidate 2026-09"
    changed["profile"]["email"] = "candidate@example.com"

    report = SeedImporter(repo).import_data(changed, source_name="career_seed.json")

    assert report.updated == ["skill-java"]
    assert report.profile == "updated"
    assert "skill-python" in report.skipped
    node = EvidenceNode.model_validate(repo.require_node("skill-java"))
    assert node.verification_status is VerificationStatus.VERIFIED
    assert node.version == 2
    assert node.allowed_for_application
    event = repo.list_audit("evidence_node", "skill-java")[0]
    assert event.action == "updated" and event.actor == "importer"
    assert event.before["verification_status"] == "UNVERIFIED"
    assert event.after["verification_status"] == "VERIFIED"
    assert repo.get_profile_row().email == "candidate@example.com"
    assert repo.get_profile_row().version == 2


def test_force_reapplies_without_creating_duplicates(repo, seed_path):
    first = SeedImporter(repo).import_file(seed_path)
    forced = SeedImporter(repo).import_file(seed_path, force=True)
    assert forced.created == []
    assert len(repo.list_nodes()) == len(first.created)


def test_import_never_creates_verified_from_inferred_source(repo, seed_data):
    data = copy.deepcopy(seed_data)
    data["facts"] = [
        {
            "id": "fact-x",
            "category": "identity",
            "statement": "Inferred but marked verified",
            "source": "guess",
            "verification_status": "INFERRED",
            "confidence": 0.5,
            "allowed_for_resume": False,
            "allowed_for_application": False,
        }
    ]
    report = SeedImporter(repo).import_data(data, source_name="x.json")
    node = EvidenceNode.model_validate(repo.require_node("fact-x"))
    assert node.source_type is EvidenceSourceType.INFERRED
    assert node.verification_status is VerificationStatus.INFERRED
    assert "fact-x" in report.created


def test_import_is_scoped_to_its_tenant(repo, other_repo, seed_path):
    SeedImporter(repo).import_file(seed_path)
    assert other_repo.get_profile_row() is None
    assert other_repo.list_nodes() == []
    SeedImporter(other_repo).import_file(seed_path)
    assert other_repo.get_profile_row().id != repo.get_profile_row().id
    assert len(other_repo.list_nodes()) == len(repo.list_nodes())
