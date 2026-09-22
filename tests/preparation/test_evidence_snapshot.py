"""Evidence snapshot: only application-safe nodes back claims; selection is ordered and traceable."""

from types import SimpleNamespace

from app.career.models import EvidenceKind, EvidenceNodeUpdate
from app.models.enums import VerificationStatus
from app.pipeline.models import TailoringLevel
from app.preparation.evidence import LEVEL_LIMITS, EvidenceSnapshot, select_evidence


def _assessment(name, refs, contribution=10.0, status="MATCHED"):
    return SimpleNamespace(requirement_name=name, evidence_references=refs, contribution=contribution, status=status)


def test_safe_keys_exclude_unverified_inferred_and_removed(evidence):
    evidence.remove_node("skill-docker", actor="test", reason="no longer true")
    evidence.commit()
    snapshot = EvidenceSnapshot.load(evidence)
    assert "skill-python" in snapshot.safe_keys
    assert "skill-java" not in snapshot.safe_keys, "UNVERIFIED skill is not application-safe"
    assert "fact-inferred-location" not in snapshot.safe_keys
    assert "metric:ticket-engine:throughput" not in snapshot.safe_keys, "project-reported metric stays out"
    assert "skill-docker" not in snapshot.nodes and snapshot.is_removed("skill-docker")
    assert snapshot.profile.name == "Ribhu Siripurapu"
    assert snapshot.projects_demonstrating("skill-go") == ["ticket-engine"]


def test_fingerprint_changes_with_evidence(evidence):
    before = EvidenceSnapshot.load(evidence).fingerprint
    evidence.update_node("skill-java", EvidenceNodeUpdate(verification_status=VerificationStatus.VERIFIED), actor="candidate")
    evidence.commit()
    after = EvidenceSnapshot.load(evidence).fingerprint
    assert before != after
    assert EvidenceSnapshot.load(evidence).fingerprint == after, "stable when nothing changes"


def test_selection_orders_matched_first_and_excludes_unsafe(evidence):
    snapshot = EvidenceSnapshot.load(evidence)
    assessments = [
        _assessment("Go", ["skill-go"], 30.0),
        _assessment("Python", ["skill-python"], 20.0),
        _assessment("Java", ["skill-java"], 15.0),  # unverified: must be excluded
        _assessment("Throughput", ["metric:ticket-engine:throughput"], 5.0),
        _assessment("Ghost", ["skill-does-not-exist"], 1.0),
        _assessment("Gap", [], 0.0, status="MISSING"),
    ]
    selection = select_evidence(snapshot, assessments, None, TailoringLevel.L1)
    assert selection.skills[:2] == ["skill-go", "skill-python"]
    assert "ticket-engine" in selection.projects, "a matched skill drags in the project that demonstrates it"
    assert "skill-java" not in selection.skills
    assert selection.excluded["skill-java"].startswith("not application-safe")
    assert selection.excluded["metric:ticket-engine:throughput"].startswith("not application-safe")
    assert selection.excluded["skill-does-not-exist"] == "unknown"
    assert selection.matched_requirements["Go"] == ["skill-go"]
    assert "education:primary" in selection.education
    assert len(selection.skills) <= LEVEL_LIMITS[TailoringLevel.L1]["skills"]


def test_level_limits_and_variant_priority(evidence):
    from app.career.models import PositioningVariantCreate, PositioningVariantEvidence

    variant = evidence.create_variant(
        PositioningVariantCreate(
            role_family="ML Engineer",
            headline="ML-leaning",
            summary="TensorFlow and LSTMs.",
            evidence=[PositioningVariantEvidence(key="skill-tensorflow", position=0), PositioningVariantEvidence(key="plant-disease", position=1)],
        ),
        actor="test",
    )
    evidence.commit()
    snapshot = EvidenceSnapshot.load(evidence)
    selection = select_evidence(snapshot, [], variant, TailoringLevel.L0)
    assert selection.skills[0] == "skill-tensorflow" and selection.projects[0] == "plant-disease"
    assert len(selection.projects) <= LEVEL_LIMITS[TailoringLevel.L0]["projects"]
    assert all(snapshot.nodes[k].kind == EvidenceKind.SKILL.value for k in selection.skills)
