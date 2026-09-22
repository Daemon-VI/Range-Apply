"""Evidence snapshot and selection.

``EvidenceSnapshot`` loads one tenant's Career Brain once (four queries) and
answers every question the composer, resolver and validator ask in memory,
so preparing a thousand opportunities never touches the evidence tables a
thousand times. Only *application-safe* nodes (grade CONFIRMED and
``allowed_for_application``) may back a factual claim; everything else is
visible for explanation and excluded for claims.

``select_evidence`` picks and orders evidence for one opportunity from the
Phase 3 requirement assessments (stable ``Skill.id``/``Project.id`` keys),
the chosen positioning variant, and the remaining confirmed evidence.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.career.database.models import (
    AnswerBankEntryRow,
    CandidateProfileRow,
    EvidenceNodeRow,
    EvidenceRelationshipRow,
    PositioningVariantRow,
)
from app.career.models import EvidenceGrade, EvidenceKind, EvidenceStatus, RelationType, grade_for
from app.career.read_model import preferences_from_row, profile_from_row
from app.career.repository import EvidenceRepository, normalize_question
from app.intelligence.taxonomy import find_skills_in_text
from app.models.enums import VerificationStatus
from app.models.preference import Preference
from app.models.profile import Profile
from app.pipeline.models import TailoringLevel

#: Numbers/percentages/years as they appear in text; shared with the validator.
#: A digit group may contain thousands separators but never ends on one: "SQLAlchemy 2, Alembic"
#: is the number "2", not "2," (first real dry run: the comma made a truthful block "unsupported").
NUMBER_PATTERN = re.compile(r"(?<![\w.])\d(?:[\d,]*\d)?(?:\.\d+)?\+?%?")

#: Per-level caps on how much evidence a package carries. L0 is the lean,
#: maximum-volume package; L2 the fullest deterministic one.
LEVEL_LIMITS: dict[TailoringLevel, dict[str, int]] = {
    TailoringLevel.L0: {"skills": 10, "projects": 2, "experiences": 2, "responsibilities": 2, "metrics": 2},
    TailoringLevel.L1: {"skills": 14, "projects": 3, "experiences": 3, "responsibilities": 3, "metrics": 3},
    TailoringLevel.L2: {"skills": 18, "projects": 4, "experiences": 3, "responsibilities": 5, "metrics": 4},
}


class EvidenceSnapshot:
    def __init__(
        self,
        tenant_id: str,
        nodes: list[EvidenceNodeRow],
        removed_keys: set[str],
        relationships: list[EvidenceRelationshipRow],
        profile_row: Optional[CandidateProfileRow],
        variants: list[PositioningVariantRow],
        answers: list[AnswerBankEntryRow],
    ):
        self.tenant_id = tenant_id
        self.nodes: dict[str, EvidenceNodeRow] = {n.key: n for n in nodes}
        self.removed_keys = removed_keys
        self._by_id = {n.id: n for n in nodes}
        self.safe_keys: set[str] = {
            n.key
            for n in nodes
            if grade_for(VerificationStatus(n.verification_status)) is EvidenceGrade.CONFIRMED
            and n.allowed_for_application
        }
        self.children: dict[str, list[tuple[str, str, int]]] = {}
        self.parents: dict[str, list[tuple[str, str]]] = {}
        for rel in relationships:
            parent = self._by_id.get(rel.from_node_id)
            child = self._by_id.get(rel.to_node_id)
            if parent is None or child is None:
                continue
            self.children.setdefault(parent.key, []).append((rel.relation, child.key, rel.position))
            self.parents.setdefault(child.key, []).append((rel.relation, parent.key))
        for lst in self.children.values():
            lst.sort(key=lambda item: item[2])
        self.profile_row = profile_row
        self.profile: Optional[Profile] = profile_from_row(profile_row) if profile_row else None
        self.preferences: Optional[Preference] = preferences_from_row(profile_row) if profile_row else None
        self.variants = [v for v in variants if v.is_active]
        self.answers = answers
        self.answer_by_key: dict[str, AnswerBankEntryRow] = {a.question_key: a for a in answers}
        self.answers_by_category: dict[str, list[AnswerBankEntryRow]] = {}
        for a in answers:
            self.answers_by_category.setdefault(a.category, []).append(a)
        self.fingerprint = self._fingerprint()
        # Per-node text, numbers and taxonomy skills, computed once so the
        # validator never rescans evidence per block (blueprint §11 batching).
        self.node_text: dict[str, str] = {}
        self.node_numbers: dict[str, set[str]] = {}
        self.node_skills: dict[str, set[str]] = {}
        for key, node in self.nodes.items():
            text = "\n".join(
                [node.label, node.claim, json.dumps(node.attributes or {}, ensure_ascii=False, default=str)]
            )
            lowered = text.lower()
            self.node_text[key] = lowered
            self.node_numbers[key] = set(NUMBER_PATTERN.findall(lowered))
            self.node_skills[key] = set(find_skills_in_text(text))

    # ------------------------------------------------------------------ #

    @classmethod
    def load(cls, repo: EvidenceRepository) -> "EvidenceSnapshot":
        all_nodes = repo.list_nodes(include_removed=True)
        active = [n for n in all_nodes if n.status == EvidenceStatus.ACTIVE.value]
        removed = {n.key for n in all_nodes if n.status != EvidenceStatus.ACTIVE.value}
        from app.career.models import AnswerStatus

        return cls(
            tenant_id=repo.tenant_id,
            nodes=active,
            removed_keys=removed,
            relationships=repo.list_relationships(),
            profile_row=repo.get_profile_row(),
            variants=repo.list_variants(active_only=True),
            answers=repo.list_answers(status=AnswerStatus.APPROVED),
        )

    def _fingerprint(self) -> str:
        digest = hashlib.sha256()
        for key in sorted(self.nodes):
            node = self.nodes[key]
            digest.update(f"{key}:{node.version}:{node.verification_status}:{int(node.allowed_for_application)}\n".encode())
        digest.update(f"profile:{self.profile_row.version if self.profile_row else 0}\n".encode())
        for v in sorted(self.variants, key=lambda v: v.id):
            digest.update(f"variant:{v.id}:{v.version}\n".encode())
        for a in sorted(self.answers, key=lambda a: a.id):
            digest.update(f"answer:{a.id}:{a.version}\n".encode())
        return digest.hexdigest()

    # ------------------------------------------------------------------ #

    def node(self, key: str) -> Optional[EvidenceNodeRow]:
        return self.nodes.get(key)

    def is_safe(self, key: str) -> bool:
        return key in self.safe_keys

    def is_removed(self, key: str) -> bool:
        return key in self.removed_keys

    def of_kind(self, kind: EvidenceKind, safe_only: bool = True) -> list[EvidenceNodeRow]:
        rows = [
            n
            for n in self.nodes.values()
            if n.kind == kind.value and (not safe_only or n.key in self.safe_keys)
        ]
        rows.sort(key=lambda n: (n.sort_order, n.created_at or 0))
        return rows

    def safe_children(self, parent_key: str, relation: RelationType) -> list[EvidenceNodeRow]:
        return [
            self.nodes[child_key]
            for rel, child_key, _ in self.children.get(parent_key, [])
            if rel == relation.value and child_key in self.safe_keys
        ]

    def projects_demonstrating(self, skill_key: str) -> list[str]:
        return [
            parent_key
            for rel, parent_key in self.parents.get(skill_key, [])
            if rel == RelationType.DEMONSTRATES.value and parent_key in self.safe_keys
        ]

    def skill_by_label(self, label: str) -> Optional[EvidenceNodeRow]:
        wanted = label.strip().lower()
        for node in self.of_kind(EvidenceKind.SKILL):
            if node.label.strip().lower() == wanted:
                return node
        return None

    def supporting_text(self, keys: Iterable[str]) -> str:
        """Everything the cited nodes say, for the validator's number/skill checks."""
        parts = []
        for key in keys:
            node = self.nodes.get(key)
            if node is None:
                continue
            parts.append(node.label)
            parts.append(node.claim)
            parts.append(json.dumps(node.attributes or {}, ensure_ascii=False, default=str))
        return "\n".join(parts)

    def find_answer(self, question: str) -> Optional[AnswerBankEntryRow]:
        return self.answer_by_key.get(normalize_question(question))


@dataclass
class EvidenceSelection:
    skills: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    experiences: list[str] = field(default_factory=list)
    education: list[str] = field(default_factory=list)
    #: requirement name -> evidence keys that satisfied it (application-safe only)
    matched_requirements: dict[str, list[str]] = field(default_factory=dict)
    #: keys that Phase 3 cited but that may not back a claim, with the reason
    excluded: dict[str, str] = field(default_factory=dict)

    @property
    def all_keys(self) -> list[str]:
        seen: list[str] = []
        for key in self.skills + self.projects + self.experiences + self.education:
            if key not in seen:
                seen.append(key)
        return seen


def select_evidence(
    snapshot: EvidenceSnapshot,
    assessments: list[Any],
    variant: Optional[PositioningVariantRow],
    level: TailoringLevel,
) -> EvidenceSelection:
    """Order application-safe evidence for one opportunity.

    Priority: evidence that satisfied a job requirement (by contribution),
    then the positioning variant's chosen evidence, then the remaining
    confirmed evidence, capped per level. Nothing unsafe ever enters the
    selection; it is listed in ``excluded`` with the reason.
    """
    limits = LEVEL_LIMITS[level]
    selection = EvidenceSelection()
    seen: set[str] = set()

    def consider(key: str, requirement: Optional[str] = None) -> None:
        if key in seen:
            if requirement:
                selection.matched_requirements.setdefault(requirement, []).append(key)
            return
        node = snapshot.node(key)
        if node is None:
            selection.excluded[key] = "removed" if snapshot.is_removed(key) else "unknown"
            return
        if not snapshot.is_safe(key):
            selection.excluded[key] = f"not application-safe ({node.verification_status})"
            return
        seen.add(key)
        if requirement:
            selection.matched_requirements.setdefault(requirement, []).append(key)
        kind = EvidenceKind(node.kind)
        if kind is EvidenceKind.SKILL:
            selection.skills.append(key)
        elif kind is EvidenceKind.PROJECT:
            selection.projects.append(key)
        elif kind is EvidenceKind.EXPERIENCE:
            selection.experiences.append(key)
        elif kind is EvidenceKind.EDUCATION:
            selection.education.append(key)

    ordered = sorted(
        [a for a in assessments if a.status in ("MATCHED", "PARTIAL")],
        key=lambda a: -(a.contribution or 0.0),
    )
    for assessment in ordered:
        for key in assessment.evidence_references or []:
            consider(key, assessment.requirement_name)
            # A matched skill drags in the projects that demonstrate it.
            if key in snapshot.safe_keys and snapshot.nodes[key].kind == EvidenceKind.SKILL.value:
                for project_key in snapshot.projects_demonstrating(key):
                    consider(project_key)

    if variant is not None:
        for entry in sorted(variant.evidence, key=lambda e: e.position):
            consider(entry.node.key)

    for node in snapshot.of_kind(EvidenceKind.SKILL):
        consider(node.key)
    for node in snapshot.of_kind(EvidenceKind.PROJECT):
        consider(node.key)
    for node in snapshot.of_kind(EvidenceKind.EXPERIENCE):
        consider(node.key)
    for node in snapshot.of_kind(EvidenceKind.EDUCATION):
        consider(node.key)

    selection.skills = selection.skills[: limits["skills"]]
    selection.projects = selection.projects[: limits["projects"]]
    selection.experiences = selection.experiences[: limits["experiences"]]
    return selection
