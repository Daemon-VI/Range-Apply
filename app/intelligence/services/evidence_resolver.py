"""Resolves job requirements against Career Brain evidence.

Evidence strength is ordered by how defensible a claim would be in an actual
application (FR-05):

* ``DIRECT_VERIFIED`` — a verified skill in the Career Brain.
* ``STRONG_DEMONSTRATED`` — used in a real project.
* ``SUPPORTING`` — an unverified self-declared skill, or a lexical variant.
* ``ADJACENT`` / ``INDIRECT`` — related technology or semantic similarity.
* ``NONE`` — no evidence; the requirement is a genuine gap.

The Career Brain stays the single source of truth: nothing here invents or
infers a capability the candidate has not recorded.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from pydantic import BaseModel

from app.intelligence.matching.skill_matcher import SkillMatcher
from app.intelligence.models.enums import EvidenceStrength
from app.models import Experience, Preference
from app.services.career_brain import CareerBrainService


def _months_between(start: Optional[str], end: Optional[str]) -> Optional[int]:
    """Months between two loosely formatted dates ('2025-06', '2025', 'Present')."""
    import re
    from datetime import date

    def parse(value: Optional[str]) -> Optional[date]:
        if not value:
            return None
        text = value.strip().lower()
        if text in ("present", "current", "ongoing", "now"):
            return date.today()
        match = re.match(r"(\d{4})(?:[-/](\d{1,2}))?", text)
        if not match:
            return None
        year = int(match.group(1))
        month = int(match.group(2) or 1)
        if not 1 <= month <= 12:
            month = 1
        return date(year, month, 1)

    start_date = parse(start)
    if start_date is None:
        return None
    end_date = parse(end) or date.today()
    months = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month)
    return max(0, months)


class ResolvedEvidence(BaseModel):
    strength: EvidenceStrength
    references: List[str] = []
    description: str
    method: str = "none"
    similarity: float = 0.0


@dataclass
class _SkillIndex:
    """Candidate vocabulary, built once per matching run."""

    verified_skills: Dict[str, str]  # lowercase name -> skill id
    unverified_skills: Dict[str, str]
    project_tech: Dict[str, List[str]]  # lowercase tech -> project ids
    experience_tech: Dict[str, List[str]]  # lowercase tech -> experience ids

    @property
    def all_terms(self) -> List[str]:
        return sorted(
            {
                *self.verified_skills,
                *self.unverified_skills,
                *self.project_tech,
                *self.experience_tech,
            }
        )


class EvidenceResolver:
    """Resolves career brain data against specific job requirements."""

    def __init__(
        self,
        career_brain: CareerBrainService,
        matcher: Optional[SkillMatcher] = None,
    ):
        self.career_brain = career_brain
        self.matcher = matcher or SkillMatcher()
        self._index: Optional[_SkillIndex] = None

    def _build_index(self) -> _SkillIndex:
        if self._index is not None:
            return self._index

        verified: Dict[str, str] = {}
        unverified: Dict[str, str] = {}
        for skill in self.career_brain.get_skills():
            target = verified if skill.is_verified else unverified
            target[skill.name.lower()] = skill.id

        project_tech: Dict[str, List[str]] = {}
        for project in self.career_brain.get_projects():
            for tech in project.technologies:
                project_tech.setdefault(tech.lower(), []).append(project.id)

        experience_tech: Dict[str, List[str]] = {}
        for experience in self.career_brain.get_experience():
            for tech in experience.technologies:
                experience_tech.setdefault(tech.lower(), []).append(experience.id)

        self._index = _SkillIndex(verified, unverified, project_tech, experience_tech)
        return self._index

    def resolve_skill(self, skill_name: str) -> ResolvedEvidence:
        """Look a requirement up across skills and project technologies."""
        index = self._build_index()

        result = self.matcher.match(skill_name, index.all_terms)
        if not result.is_match:
            return ResolvedEvidence(
                strength=EvidenceStrength.NONE,
                references=[],
                description="No evidence found in the Career Brain.",
                method="none",
            )

        matched = (result.matched_skill or "").lower()

        # An adjacency hit is never evidence *for* the requirement itself, only
        # a related capability. Reporting it as "verified skill: Go" when the
        # requirement was Kubernetes would overstate what the candidate can claim.
        if result.method in ("related", "semantic"):
            references = (
                index.verified_skills.get(matched)
                or index.unverified_skills.get(matched)
                or (index.project_tech.get(matched) or [None])[0]
            )
            return ResolvedEvidence(
                strength=result.strength,
                references=[references] if references else [],
                description=(
                    f"No direct evidence for '{skill_name}'; adjacent experience with "
                    f"'{result.matched_skill}' ({result.method})."
                ),
                method=result.method,
                similarity=result.similarity,
            )

        # A verified skill is the strongest claim we can make.
        if matched in index.verified_skills:
            strength = (
                EvidenceStrength.DIRECT_VERIFIED
                if result.method in ("exact", "alias")
                else EvidenceStrength.SUPPORTING
            )
            references = [index.verified_skills[matched]]
            description = f"Verified skill: {result.matched_skill}"
            if matched in index.project_tech:
                references.extend(index.project_tech[matched])
                description += f" (used in {len(index.project_tech[matched])} project(s))"
            return ResolvedEvidence(
                strength=strength,
                references=references,
                description=description,
                method=result.method,
                similarity=result.similarity,
            )

        # Demonstrated in a project, even if not listed as a standalone skill.
        if matched in index.project_tech:
            projects = index.project_tech[matched]
            strength = (
                EvidenceStrength.STRONG_DEMONSTRATED
                if result.method in ("exact", "alias")
                else EvidenceStrength.SUPPORTING
            )
            return ResolvedEvidence(
                strength=strength,
                references=projects,
                description=f"Demonstrated in project(s): {', '.join(projects)}",
                method=result.method,
                similarity=result.similarity,
            )

        # Used in real work experience.
        if matched in index.experience_tech:
            experiences = index.experience_tech[matched]
            return ResolvedEvidence(
                strength=EvidenceStrength.STRONG_DEMONSTRATED
                if result.method in ("exact", "alias")
                else EvidenceStrength.SUPPORTING,
                references=experiences,
                description=f"Used in experience: {', '.join(experiences)}",
                method=result.method,
                similarity=result.similarity,
            )

        # Declared but unverified: usable as a signal, never as a claim.
        if matched in index.unverified_skills:
            return ResolvedEvidence(
                strength=EvidenceStrength.SUPPORTING,
                references=[index.unverified_skills[matched]],
                description=f"Declared but unverified skill: {result.matched_skill}",
                method=result.method,
                similarity=result.similarity,
            )

        return ResolvedEvidence(
            strength=result.strength,
            references=[],
            description=f"Related to '{result.matched_skill}' ({result.method})",
            method=result.method,
            similarity=result.similarity,
        )

    def resolve_experience(self) -> List[Experience]:
        return self.career_brain.get_experience()

    def resolve_preferences(self) -> Preference:
        return self.career_brain.get_preferences()

    def total_experience_months(self) -> Optional[int]:
        """Months of recorded professional experience.

        Returns ``None`` when no experience entry carries usable dates. The
        caller must treat that as *unknown*, not as "zero experience" - the
        difference decides between an UNCERTAIN and a MISSING assessment.
        """
        total = 0
        found = False
        for experience in self.career_brain.get_experience():
            if not experience.is_employment:
                continue
            months = _months_between(experience.start_date, experience.end_date)
            if months is not None:
                total += months
                found = True
        return total if found else None
