"""Selects the strongest matched requirements and resolves them to real evidence.

Truthfulness rule (PRD FR-05): every ``evidence_text`` returned here is copied
verbatim from a verified skill's ``evidence`` field or a project's own
``summary``/``solution`` text - never synthesized.
"""

from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.intelligence.database.models import JobMatchRow, RequirementAssessmentRow
from app.services.career_brain import CareerBrainService

MAX_SELECTED = 6


def _resolve_evidence_text(ref: str, career_brain: CareerBrainService) -> Optional[Tuple[str, str]]:
    """Resolve one evidence reference id to (source_name, evidence_text).

    A reference matches either a skill id or a project id in the career
    brain. Anything else resolves to nothing rather than a guess.
    """
    for skill in career_brain.get_verified_skills():
        if skill.id == ref:
            return skill.name, skill.evidence
    for project in career_brain.get_projects():
        if project.id == ref:
            text = project.solution or project.summary
            return project.name, text
    return None


def select_evidence(
    db: Session,
    job_match: JobMatchRow,
    career_brain: CareerBrainService,
) -> List[Tuple[str, str]]:
    """Rank matched requirements by contribution and resolve them to real evidence.

    Returns an ordered list of ``(requirement_name, evidence_text)`` pairs,
    at most ``MAX_SELECTED`` long, skipping requirements whose evidence
    references do not resolve to anything real.
    """
    assessments = (
        db.query(RequirementAssessmentRow)
        .filter(RequirementAssessmentRow.job_match_id == job_match.id)
        .filter(RequirementAssessmentRow.status == "MATCHED")
        .order_by(RequirementAssessmentRow.contribution.desc())
        .all()
    )

    selected: List[Tuple[str, str]] = []
    for assessment in assessments:
        if len(selected) >= MAX_SELECTED:
            break
        for ref in assessment.evidence_references or []:
            resolved = _resolve_evidence_text(ref, career_brain)
            if resolved is None:
                continue
            source_name, evidence_text = resolved
            if not evidence_text:
                continue
            selected.append((source_name, evidence_text))
            break  # one piece of evidence per requirement is enough

    return selected[:MAX_SELECTED]
