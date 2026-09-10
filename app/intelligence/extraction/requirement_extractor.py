"""Deterministic requirement extraction from a normalized job description.

Deterministic-first, per FR-03: sections, regexes and the curated taxonomy do
the work. An LLM is not consulted here at all — that stays a per-field fallback
inside Phase 2 extraction.

The extractor is **section aware**. "Kubernetes" under *Minimum Qualifications*
is a hard requirement; the same word under *Nice to have* is not; under
*Benefits* it is not a requirement at all. Flat keyword counting cannot tell
those apart, which is what made the previous fit score meaningless.

Every requirement keeps its category, strictness, confidence and the exact
source text it came from, so a match can always be traced back to the JD.
"""

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

from app.intelligence.models.enums import ConfidenceLevel, RequirementCategory, Strictness
from app.intelligence.models.requirements import StructuredRequirement
from app.intelligence.taxonomy.skills import TaxonomyProvider, default_taxonomy
from app.jobs.models.enums import EmploymentType, ExperienceLevel, RemoteType
from app.jobs.models.job import NormalizedJob


class SectionKind(str, Enum):
    """What a JD section implies about the requirements inside it."""

    REQUIRED = "REQUIRED"
    PREFERRED = "PREFERRED"
    RESPONSIBILITY = "RESPONSIBILITY"
    ELIGIBILITY = "ELIGIBILITY"
    IGNORED = "IGNORED"
    UNKNOWN = "UNKNOWN"


@dataclass
class Section:
    heading: str
    body: str
    kind: SectionKind


# Heading patterns, most specific first. Matched against a lowercased heading.
_SECTION_RULES: Tuple[Tuple[str, SectionKind], ...] = (
    (r"nice[\s-]to[\s-]have|preferred|bonus|plus(?:es)?\b|good to have|desirable|it'?s a plus", SectionKind.PREFERRED),
    (r"minimum qualification|basic qualification|required|requirements|must have|what you(?:'| )ll need|"
     r"what we(?:'| )re looking for|who you are|about you|your (?:profile|background)|qualifications|skills? (?:&|and) experience",
     SectionKind.REQUIRED),
    (r"responsibilit|what you(?:'| )ll do|the role|day[\s-]to[\s-]day|your impact|what you will be doing",
     SectionKind.RESPONSIBILITY),
    (r"eligibilit|who can apply|graduation|batch", SectionKind.ELIGIBILITY),
    (r"benefit|perks|compensation and benefits|about (?:us|the company|the team)|why join|equal opportunity|"
     r"eeo|diversity|our values|life at|how to apply|interview process|hiring process",
     SectionKind.IGNORED),
)

_HEADING_RE = re.compile(
    r"^\s*(?:[#*\-•]*\s*)?([A-Z][A-Za-z0-9 ,'&/()\-]{2,60}?)\s*:?\s*$",
    re.MULTILINE,
)

_BULLET_RE = re.compile(r"^\s*(?:[-*•●▪–]|\d+[.)])\s+(.{3,})$", re.MULTILINE)

# --- Requirement-level patterns ---------------------------------------------
_YEARS_RE = re.compile(
    r"(?P<min>\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|to)?\s*(?P<max>\d{1,2})?\s*\+?\s*"
    r"(?:years?|yrs?)\s+(?:of\s+)?(?:professional\s+|relevant\s+|industry\s+|hands[\s-]on\s+)?experience",
    re.IGNORECASE,
)
_MONTHS_RE = re.compile(r"(\d{1,2})\s*(?:\+)?\s*months?\s+(?:of\s+)?experience", re.IGNORECASE)

# The abbreviated forms require their dots: a bare `b\.?\s?e\b` alternative
# matched the word "be", turning "must be legally authorized to work" into a
# degree requirement.
_DEGREE_RE = re.compile(
    r"(?<![A-Za-z])(?P<level>bachelor'?s?|master'?s?|undergraduate degree|"
    r"b\.?tech|b\.\s?e\.?|b\.?sc|m\.?tech|m\.\s?s\.?|m\.?sc|mba|ph\.?\s?d|doctorate|associate'?s?)"
    r"(?![A-Za-z])"
    r"(?:[^.\n]{0,60}?\bin\s+(?P<field>[A-Za-z ,/&+-]{3,60}))?",
    re.IGNORECASE,
)

# Canonical degree levels, so "Bachelor's" / "B.Tech" / "BSc" collapse into one
# requirement instead of three near-duplicates.
_DEGREE_LEVELS = (
    (r"ph\.?\s?d|doctorate", "PhD"),
    (r"mba", "MBA"),
    (r"m\.?tech|m\.\s?s\.?|m\.?sc|master", "Master's"),
    (r"b\.?tech|b\.\s?e\.?|b\.?sc|bachelor|undergraduate", "Bachelor's"),
    (r"associate", "Associate's"),
)


def _degree_level(raw: str) -> str:
    """Map any degree surface form onto a canonical level label."""
    lowered = raw.lower()
    for pattern, label in _DEGREE_LEVELS:
        if re.search(pattern, lowered):
            return label
    return raw.strip().capitalize()


_DEGREE_OPTIONAL_RE = re.compile(
    r"\b(?:degree\s+(?:is\s+)?(?:preferred|a plus|not required)|or equivalent(?: experience| practical experience)?|"
    r"equivalent practical experience)\b",
    re.IGNORECASE,
)

_WORK_AUTH_BLOCKING_RE = re.compile(
    r"\b(?:not\s+(?:able|be able)\s+to\s+sponsor|do(?:es)?\s+not\s+(?:offer|provide)\s+(?:visa\s+)?sponsorship|"
    r"no\s+visa\s+sponsorship|without\s+sponsorship|unable to sponsor|"
    r"(?:must|required to)\s+be\s+(?:a\s+)?(?:us|u\.s\.)\s+citizen|security clearance|"
    r"must\s+(?:be\s+)?(?:legally\s+)?authorized\s+to\s+work)\b",
    re.IGNORECASE,
)
_WORK_AUTH_FRIENDLY_RE = re.compile(
    r"\b(?:visa\s+sponsorship\s+(?:is\s+)?available|we\s+sponsor|sponsorship\s+(?:is\s+)?(?:offered|provided))\b",
    re.IGNORECASE,
)

_ONSITE_HARD_RE = re.compile(
    r"\b(?:must\s+(?:be\s+)?(?:located|based)\s+in|required\s+to\s+(?:be\s+)?(?:in|on[\s-]site)|"
    r"relocat(?:e|ion)\s+(?:to|required)|this\s+role\s+is\s+(?:fully\s+)?on[\s-]site)\b",
    re.IGNORECASE,
)


def _classify_heading(heading: str) -> SectionKind:
    lowered = heading.lower()
    for pattern, kind in _SECTION_RULES:
        if re.search(pattern, lowered):
            return kind
    return SectionKind.UNKNOWN


def split_sections(text: str) -> List[Section]:
    """Split a plain-text JD into labelled sections.

    Headings are short standalone lines. Text before the first heading becomes
    an ``UNKNOWN`` section so nothing is silently dropped.
    """
    if not text or not text.strip():
        return []

    matches = [m for m in _HEADING_RE.finditer(text) if _classify_heading(m.group(1)) != SectionKind.UNKNOWN]

    if not matches:
        return [Section(heading="", body=text, kind=SectionKind.UNKNOWN)]

    sections: List[Section] = []
    preamble = text[: matches[0].start()].strip()
    if preamble:
        sections.append(Section(heading="", body=preamble, kind=SectionKind.UNKNOWN))

    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections.append(Section(heading=heading, body=body, kind=_classify_heading(heading)))

    return sections


def _bullets(body: str) -> List[str]:
    """Bullet lines if present, otherwise sentences, so context is preserved."""
    bullets = [b.strip() for b in _BULLET_RE.findall(body) if b.strip()]
    if bullets:
        return bullets
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", body) if len(s.strip()) > 15]


def _strictness_for(kind: SectionKind) -> Strictness:
    return {
        SectionKind.REQUIRED: Strictness.REQUIRED,
        SectionKind.PREFERRED: Strictness.PREFERRED,
        SectionKind.RESPONSIBILITY: Strictness.SIGNAL,
        SectionKind.ELIGIBILITY: Strictness.HARD,
        SectionKind.UNKNOWN: Strictness.SIGNAL,
    }.get(kind, Strictness.SIGNAL)


def _confidence_for(kind: SectionKind) -> ConfidenceLevel:
    if kind in (SectionKind.REQUIRED, SectionKind.PREFERRED, SectionKind.ELIGIBILITY):
        return ConfidenceLevel.HIGH
    if kind is SectionKind.RESPONSIBILITY:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


_STRICTNESS_RANK = {
    Strictness.HARD: 5,
    Strictness.REQUIRED: 4,
    Strictness.PREFERRED: 3,
    Strictness.NICE_TO_HAVE: 2,
    Strictness.SIGNAL: 1,
    Strictness.UNKNOWN: 0,
}


def _requirement_id(category: RequirementCategory, name: str) -> str:
    """Stable id: the same requirement gets the same id across runs.

    Random UUIDs would make two runs over an unchanged job produce
    "different" requirements, breaking score reproducibility.
    """
    digest = hashlib.sha256(f"{category.value}|{name.lower()}".encode()).hexdigest()
    return f"req-{digest[:16]}"


class RequirementExtractor:
    """Turns a NormalizedJob into typed, traceable requirements."""

    def __init__(self, taxonomy: Optional[TaxonomyProvider] = None):
        self.taxonomy = taxonomy or default_taxonomy()

    def extract(self, job: NormalizedJob) -> List[StructuredRequirement]:
        """Extract every requirement, strongest classification wins on conflict."""
        collected: Dict[Tuple[str, str], StructuredRequirement] = {}

        def add(requirement: StructuredRequirement) -> None:
            key = (requirement.category.value, requirement.normalized_name.lower())
            existing = collected.get(key)
            if existing is None or _STRICTNESS_RANK[requirement.strictness] > _STRICTNESS_RANK[
                existing.strictness
            ]:
                collected[key] = requirement

        text = job.description or job.original_description or ""
        sections = split_sections(text)

        for section in sections:
            if section.kind is SectionKind.IGNORED:
                continue
            for requirement in self._from_section(section):
                add(requirement)

        for requirement in self._structural_requirements(job):
            add(requirement)

        # Phase 2's own skill lists act as a floor: if the description was
        # empty or unparseable, we still surface what discovery found.
        if not any(r.category is RequirementCategory.TECHNICAL_SKILL for r in collected.values()):
            for skill in job.required_skills:
                canonical = self.taxonomy.canonicalize(skill) or skill
                add(
                    StructuredRequirement(
                        id=_requirement_id(RequirementCategory.TECHNICAL_SKILL, canonical),
                        original_text=skill,
                        normalized_name=canonical,
                        category=RequirementCategory.TECHNICAL_SKILL,
                        strictness=Strictness.REQUIRED,
                        confidence=ConfidenceLevel.MEDIUM,
                    )
                )

        return sorted(
            collected.values(),
            key=lambda r: (-_STRICTNESS_RANK[r.strictness], r.category.value, r.normalized_name),
        )

    # ------------------------------------------------------------------ #

    def _from_section(self, section: Section) -> List[StructuredRequirement]:
        requirements: List[StructuredRequirement] = []
        strictness = _strictness_for(section.kind)
        confidence = _confidence_for(section.kind)

        for line in _bullets(section.body):
            # Skills mentioned in this line.
            for canonical in self.taxonomy.find_in_text(line):
                category = (
                    RequirementCategory.RESPONSIBILITY
                    if section.kind is SectionKind.RESPONSIBILITY
                    else RequirementCategory.TECHNICAL_SKILL
                )
                if category is RequirementCategory.RESPONSIBILITY:
                    # A tool named in a responsibility is still a skill signal.
                    category = RequirementCategory.TECHNICAL_SKILL

                requirements.append(
                    StructuredRequirement(
                        id=_requirement_id(category, canonical),
                        original_text=line[:400],
                        normalized_name=canonical,
                        category=category,
                        strictness=strictness,
                        alternatives=list(self.taxonomy.related(canonical)),
                        confidence=confidence,
                    )
                )

            requirements.extend(self._experience_requirement(line, strictness, confidence))
            requirements.extend(self._education_requirement(line, strictness, confidence))
            requirements.extend(self._work_authorization_requirement(line))
            requirements.extend(self._location_requirement(line))

        return requirements

    def _experience_requirement(
        self, line: str, strictness: Strictness, confidence: ConfidenceLevel
    ) -> List[StructuredRequirement]:
        match = _YEARS_RE.search(line)
        months: Optional[int] = None
        label: Optional[str] = None

        if match:
            minimum = int(match.group("min"))
            months = minimum * 12
            label = f"{minimum}+ years experience"
        else:
            month_match = _MONTHS_RE.search(line)
            if month_match:
                months = int(month_match.group(1))
                label = f"{months} months experience"

        if months is None or label is None:
            return []

        return [
            StructuredRequirement(
                id=_requirement_id(RequirementCategory.EXPERIENCE, label),
                original_text=line[:400],
                normalized_name=label,
                category=RequirementCategory.EXPERIENCE,
                strictness=strictness,
                experience_duration_months=months,
                confidence=confidence,
            )
        ]

    def _education_requirement(
        self, line: str, strictness: Strictness, confidence: ConfidenceLevel
    ) -> List[StructuredRequirement]:
        match = _DEGREE_RE.search(line)
        if not match:
            return []

        level = _degree_level(match.group("level"))
        field_of_study = (match.group("field") or "").strip().rstrip(".,")
        # "...in Computer Science or equivalent experience" - the field ends at
        # the alternative clause, which is handled separately as a downgrade.
        field_of_study = re.split(r"\s+or\s+|\s+and\s+equivalent", field_of_study)[0].strip()
        name = f"{level} degree" + (f" in {field_of_study}" if field_of_study else "")

        # "or equivalent experience" downgrades a hard degree gate.
        effective = strictness
        if _DEGREE_OPTIONAL_RE.search(line):
            effective = Strictness.PREFERRED

        return [
            StructuredRequirement(
                id=_requirement_id(RequirementCategory.EDUCATION, name),
                original_text=line[:400],
                normalized_name=name,
                category=RequirementCategory.EDUCATION,
                strictness=effective,
                confidence=confidence,
            )
        ]

    def _work_authorization_requirement(self, line: str) -> List[StructuredRequirement]:
        if _WORK_AUTH_FRIENDLY_RE.search(line):
            return [
                StructuredRequirement(
                    id=_requirement_id(RequirementCategory.WORK_AUTHORIZATION, "sponsorship available"),
                    original_text=line[:400],
                    normalized_name="Sponsorship available",
                    category=RequirementCategory.WORK_AUTHORIZATION,
                    strictness=Strictness.SIGNAL,
                    confidence=ConfidenceLevel.MEDIUM,
                )
            ]

        if _WORK_AUTH_BLOCKING_RE.search(line):
            return [
                StructuredRequirement(
                    id=_requirement_id(RequirementCategory.WORK_AUTHORIZATION, "work authorization"),
                    original_text=line[:400],
                    normalized_name="Work authorization required",
                    category=RequirementCategory.WORK_AUTHORIZATION,
                    # HARD, but eligibility resolves it to UNCERTAIN unless the
                    # Career Brain states the candidate's authorization.
                    strictness=Strictness.HARD,
                    confidence=ConfidenceLevel.MEDIUM,
                )
            ]
        return []

    def _location_requirement(self, line: str) -> List[StructuredRequirement]:
        if not _ONSITE_HARD_RE.search(line):
            return []
        return [
            StructuredRequirement(
                id=_requirement_id(RequirementCategory.LOCATION, "on-site presence"),
                original_text=line[:400],
                normalized_name="On-site presence required",
                category=RequirementCategory.LOCATION,
                strictness=Strictness.HARD,
                confidence=ConfidenceLevel.MEDIUM,
            )
        ]

    def _structural_requirements(self, job: NormalizedJob) -> List[StructuredRequirement]:
        """Requirements that come from normalized fields rather than prose."""
        requirements: List[StructuredRequirement] = []

        grad = job.graduation_requirement
        if grad or job.graduation_year_requirement:
            name = _graduation_label(job)
            requirements.append(
                StructuredRequirement(
                    id=_requirement_id(RequirementCategory.ELIGIBILITY, name),
                    original_text=(grad.original_text if grad and grad.original_text else name),
                    normalized_name=name,
                    category=RequirementCategory.ELIGIBILITY,
                    strictness=Strictness.HARD,
                    confidence=_grad_confidence(grad.extraction_confidence if grad else 1.0),
                )
            )

        if job.experience_level is not ExperienceLevel.UNKNOWN:
            requirements.append(
                StructuredRequirement(
                    id=_requirement_id(RequirementCategory.EXPERIENCE, job.experience_level.value),
                    original_text=f"Seniority: {job.experience_level.value}",
                    normalized_name=job.experience_level.value,
                    category=RequirementCategory.EXPERIENCE,
                    strictness=Strictness.SIGNAL,
                    confidence=ConfidenceLevel.MEDIUM,
                )
            )

        if job.remote_type is not RemoteType.UNKNOWN:
            requirements.append(
                StructuredRequirement(
                    id=_requirement_id(RequirementCategory.LOCATION, job.remote_type.value),
                    original_text=f"Work mode: {job.remote_type.value}",
                    normalized_name=job.remote_type.value,
                    category=RequirementCategory.LOCATION,
                    strictness=Strictness.SIGNAL,
                    confidence=ConfidenceLevel.HIGH,
                )
            )

        if job.employment_type is not EmploymentType.UNKNOWN:
            requirements.append(
                StructuredRequirement(
                    id=_requirement_id(RequirementCategory.PREFERENCE, job.employment_type.value),
                    original_text=f"Employment type: {job.employment_type.value}",
                    normalized_name=job.employment_type.value,
                    category=RequirementCategory.PREFERENCE,
                    strictness=Strictness.SIGNAL,
                    confidence=ConfidenceLevel.HIGH,
                )
            )

        if job.location:
            requirements.append(
                StructuredRequirement(
                    id=_requirement_id(RequirementCategory.LOCATION, job.location),
                    original_text=f"Location: {job.location}",
                    normalized_name=job.location,
                    category=RequirementCategory.LOCATION,
                    strictness=Strictness.SIGNAL,
                    confidence=ConfidenceLevel.HIGH,
                )
            )

        return requirements


def _graduation_label(job: NormalizedJob) -> str:
    grad = job.graduation_requirement
    if grad is None:
        return f"Graduation {job.graduation_year_requirement}"
    if grad.exact_years:
        years = ", ".join(str(y) for y in grad.exact_years)
        return f"Graduation year in {years}"
    if grad.minimum_year and grad.maximum_year:
        return f"Graduation {grad.minimum_year}-{grad.maximum_year}"
    if grad.minimum_year:
        return f"Graduation {grad.minimum_year} or later"
    if grad.maximum_year:
        return f"Graduation by {grad.maximum_year}"
    return "Graduation requirement"


def _grad_confidence(score: float) -> ConfidenceLevel:
    if score >= 0.9:
        return ConfidenceLevel.HIGH
    if score >= 0.7:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW
