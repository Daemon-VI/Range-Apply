"""Rebuilds the Phase 1 domain models from Evidence Graph rows.

``CareerBrainService`` hands these to every existing consumer (eligibility,
evidence resolution, tailoring, the v1 API) unchanged in shape. The ``id`` of
each domain object is the node ``key``, so evidence references recorded by
Phase 3 (``RequirementAssessmentRow.evidence_references``) and Phase 4
(``TailoredArtifactRow.evidence_refs``) keep resolving.

Nothing here invents data: every field is read from a node, its attributes,
or a relationship the importer or the candidate created.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from app.career.database.models import (
    CandidateProfileRow,
    EvidenceNodeRow,
    EvidenceRelationshipRow,
)
from app.career.models import EvidenceKind, RelationType
from app.career.repository import EvidenceRepository
from app.core.timeutils import from_db
from app.jobs.geography import GeographyPolicy, policy_from_preferences
from app.models import (
    Achievement,
    CareerFact,
    Experience,
    Preference,
    Profile,
    Project,
    Skill,
)
from app.models.enums import FactCategory, SkillCategory, VerificationStatus
from app.models.project import ProjectMetric


def profile_from_row(row: CandidateProfileRow) -> Profile:
    return Profile(
        name=row.name,
        email=row.email,
        phone=row.phone,
        location=row.location,
        work_authorization=row.work_authorization,
        degree=row.degree,
        branch=row.branch,
        college=row.college,
        graduation_year=row.graduation_year,
        current_academic_status=row.current_academic_status,
        cgpa=row.cgpa,
        backlogs=row.backlogs,
        github=row.github,
        linkedin=row.linkedin,
        portfolio=row.portfolio,
        positioning_statement=row.positioning_statement or "",
        long_term_goal=row.long_term_goal or "",
    )


def preferences_from_row(row: CandidateProfileRow) -> Preference:
    return Preference(**(row.preferences or {}))


def geography_policy_from_row(row: Optional[CandidateProfileRow]) -> GeographyPolicy:
    """The tenant's job-market target: preference primary, else the profile location."""
    if row is None:
        return GeographyPolicy()
    return policy_from_preferences(preferences_from_row(row), row.location)


def load_preferences(db, tenant_id: str) -> Optional[Preference]:
    row = db.query(CandidateProfileRow).filter(CandidateProfileRow.tenant_id == tenant_id).first()
    return preferences_from_row(row) if row is not None else None


def load_geography_policy(db, tenant_id: str) -> GeographyPolicy:
    row = db.query(CandidateProfileRow).filter(CandidateProfileRow.tenant_id == tenant_id).first()
    return geography_policy_from_row(row)


def _status(row: EvidenceNodeRow) -> VerificationStatus:
    return VerificationStatus(row.verification_status)


def _attr(row: EvidenceNodeRow, name: str, default: Any = None) -> Any:
    value = (row.attributes or {}).get(name)
    return default if value is None else value


def skill_from_node(row: EvidenceNodeRow, project_keys: list[str]) -> Skill:
    category_raw = _attr(row, "category", SkillCategory.OTHER.value)
    try:
        category = SkillCategory(category_raw)
    except ValueError:
        category = SkillCategory.OTHER
    return Skill(
        id=row.key,
        name=row.label,
        category=category,
        evidence=_attr(row, "evidence", ""),
        projects=list(project_keys),
        verification_status=_status(row),
        resume_relevance=_attr(row, "resume_relevance", "medium"),
        notes=_attr(row, "notes", ""),
    )


def metric_from_node(row: EvidenceNodeRow) -> ProjectMetric:
    return ProjectMetric(
        name=_attr(row, "name", row.label),
        value=str(_attr(row, "value", "")),
        verification_status=_status(row),
    )


def project_from_node(row: EvidenceNodeRow, metrics: list[EvidenceNodeRow]) -> Project:
    return Project(
        id=row.key,
        name=row.label,
        status=_attr(row, "status", "UNKNOWN"),
        summary=row.claim,
        problem=_attr(row, "problem", ""),
        solution=_attr(row, "solution", ""),
        technologies=list(_attr(row, "technologies", [])),
        primary_domain=_attr(row, "primary_domain", ""),
        relevant_roles=list(_attr(row, "relevant_roles", [])),
        features=list(_attr(row, "features", [])),
        metrics=[metric_from_node(m) for m in metrics],
        technical_concepts=list(_attr(row, "technical_concepts", [])),
        resume_worthy_facts=list(_attr(row, "resume_worthy_facts", [])),
        verified_claims=list(_attr(row, "verified_claims", [])),
        unverified_claims=list(_attr(row, "unverified_claims", [])),
        documentation_path=_attr(row, "documentation_path", ""),
    )


def experience_from_node(row: EvidenceNodeRow, responsibilities: list[EvidenceNodeRow]) -> Experience:
    return Experience(
        id=row.key,
        organization=_attr(row, "organization", ""),
        role=_attr(row, "role", row.label),
        start_date=_attr(row, "start_date"),
        end_date=_attr(row, "end_date"),
        is_employment=bool(_attr(row, "is_employment", False)),
        responsibilities=[r.claim for r in responsibilities],
        technologies=list(_attr(row, "technologies", [])),
        achievements=list(_attr(row, "achievements", [])),
        verified_claims=list(_attr(row, "verified_claims", [])),
        verification_status=_status(row),
        notes=_attr(row, "notes", ""),
    )


def achievement_from_node(row: EvidenceNodeRow) -> Achievement:
    return Achievement(
        id=row.key,
        title=row.label,
        category=_attr(row, "category", "other"),
        description=_attr(row, "description", row.claim),
        date=_attr(row, "date"),
        verification_status=_status(row),
        related_project=_attr(row, "related_project"),
        notes=_attr(row, "notes", ""),
    )


def fact_from_node(row: EvidenceNodeRow) -> CareerFact:
    category_raw = _attr(row, "category", FactCategory.SKILL.value)
    try:
        category = FactCategory(category_raw)
    except ValueError:
        category = FactCategory.SKILL
    fact = CareerFact(
        id=row.key,
        category=category,
        statement=row.claim,
        source=_attr(row, "source", row.source_ref or ""),
        verification_status=_status(row),
        confidence=row.confidence,
        allowed_for_resume=row.allowed_for_resume,
        allowed_for_application=row.allowed_for_application,
        related_entity_id=_attr(row, "related_entity_id"),
        created_at=from_db(row.created_at),
        updated_at=from_db(row.updated_at),
    )
    return fact


@dataclass
class GraphSnapshot:
    """One tenant's active graph, materialised into the legacy domain models."""

    tenant_id: str
    profile: Optional[Profile] = None
    preferences: Optional[Preference] = None
    skills: list[Skill] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)
    experience: list[Experience] = field(default_factory=list)
    achievements: list[Achievement] = field(default_factory=list)
    facts: list[CareerFact] = field(default_factory=list)
    nodes_by_key: dict[str, EvidenceNodeRow] = field(default_factory=dict)


def _children(
    relationships: list[EvidenceRelationshipRow],
    parent: EvidenceNodeRow,
    relation: RelationType,
    nodes_by_id: dict[str, EvidenceNodeRow],
) -> list[EvidenceNodeRow]:
    rows = [
        (rel.position, nodes_by_id[rel.to_node_id])
        for rel in relationships
        if rel.from_node_id == parent.id
        and rel.relation == relation.value
        and rel.to_node_id in nodes_by_id
    ]
    rows.sort(key=lambda item: item[0])
    return [row for _, row in rows]


def _parents(
    relationships: list[EvidenceRelationshipRow],
    child: EvidenceNodeRow,
    relation: RelationType,
    nodes_by_id: dict[str, EvidenceNodeRow],
) -> list[EvidenceNodeRow]:
    rows = [
        (rel.position, nodes_by_id[rel.from_node_id])
        for rel in relationships
        if rel.to_node_id == child.id
        and rel.relation == relation.value
        and rel.from_node_id in nodes_by_id
    ]
    rows.sort(key=lambda item: item[0])
    return [row for _, row in rows]


def build_snapshot(repo: EvidenceRepository) -> GraphSnapshot:
    """Load everything active for the repository's tenant in two queries."""
    snapshot = GraphSnapshot(tenant_id=repo.tenant_id)
    profile_row = repo.get_profile_row()
    if profile_row is not None:
        snapshot.profile = profile_from_row(profile_row)
        snapshot.preferences = preferences_from_row(profile_row)

    nodes = repo.list_nodes(include_removed=False)
    nodes_by_id = {row.id: row for row in nodes}
    snapshot.nodes_by_key = {row.key: row for row in nodes}
    relationships = repo.list_relationships()

    for row in nodes:
        kind = EvidenceKind(row.kind)
        if kind is EvidenceKind.SKILL:
            demonstrated_by = _parents(relationships, row, RelationType.DEMONSTRATES, nodes_by_id)
            snapshot.skills.append(skill_from_node(row, [p.key for p in demonstrated_by]))
        elif kind is EvidenceKind.PROJECT:
            metrics = _children(relationships, row, RelationType.HAS_METRIC, nodes_by_id)
            snapshot.projects.append(project_from_node(row, metrics))
        elif kind is EvidenceKind.EXPERIENCE:
            responsibilities = _children(
                relationships, row, RelationType.HAS_RESPONSIBILITY, nodes_by_id
            )
            snapshot.experience.append(experience_from_node(row, responsibilities))
        elif kind is EvidenceKind.ACHIEVEMENT:
            snapshot.achievements.append(achievement_from_node(row))
        elif kind is EvidenceKind.FACT:
            snapshot.facts.append(fact_from_node(row))
        # EDUCATION, METRIC, RESPONSIBILITY, LINK, CREDENTIAL, OTHER nodes are
        # reachable through their parents or directly by key; the legacy
        # domain models have no standalone slot for them.
    return snapshot
