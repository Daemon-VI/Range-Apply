"""Deterministic, policy-driven fit scoring.

What changed from the previous implementation:

* ``policy_weights`` were declared and then ignored — the score was a flat
  strictness-weighted count of matched requirements. They are now the actual
  arithmetic.
* The score is decomposed into named **components** (technical, experience,
  education, preferences, eligibility), each individually reported, so a number
  can be explained instead of merely displayed.
* Requirements the engine could not determine are collected as
  **uncertainties** rather than being counted as either matches or gaps.

Reproducibility: given the same job, the same Career Brain and the same policy
version, this function returns the same numbers. There is no randomness, no
clock and no model call in the scoring path.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.intelligence.models.enums import (
    ConfidenceLevel,
    EligibilityStatus,
    MatchStatus,
    MatchType,
    RequirementCategory,
    Strictness,
)
from app.intelligence.models.requirements import RequirementAssessment
from app.intelligence.services.eligibility_engine import EligibilityEvaluation
from app.intelligence.services.preference_evaluator import PreferenceAssessment
from app.intelligence.services.role_relevance import Relevance

#: Bump when the scoring arithmetic changes so old scores stay identifiable.
POLICY_VERSION = "v1"
ENGINE_VERSION = "1.3.0"
#: Ceiling for an unread JD whose title matches no target role family (below the default MEDIUM band of 45).
UNRELATED_ROLE_FIT_CAP = 30
#: Ceiling for a potentially technical role without technical evidence (sales engineer, product builder...).
WEAK_ROLE_FIT_CAP = 55
#: Weight of the neutral prior on the technical component (one REQUIRED requirement).
TECHNICAL_EVIDENCE_PRIOR = 3.0
#: Ceiling for any JD from which no technical requirement was read (below the default HIGH band of 70).
LOW_COVERAGE_FIT_CAP = 60

DEFAULT_POLICY_WEIGHTS: Dict[str, float] = {
    "technical": 0.40,
    "experience": 0.15,
    "education": 0.10,
    "preferences": 0.20,
    "eligibility": 0.15,
}

# How much a requirement counts inside its component, by strictness.
STRICTNESS_WEIGHT: Dict[Strictness, float] = {
    Strictness.HARD: 3.0,
    Strictness.REQUIRED: 3.0,
    Strictness.PREFERRED: 1.0,
    Strictness.NICE_TO_HAVE: 0.5,
    Strictness.SIGNAL: 0.4,
    Strictness.UNKNOWN: 0.2,
}

# Credit awarded for a match of each quality.
STATUS_CREDIT: Dict[MatchStatus, float] = {
    MatchStatus.MATCHED: 1.0,
    MatchStatus.PARTIAL: 0.5,
    MatchStatus.MISSING: 0.0,
    MatchStatus.UNKNOWN: 0.0,
    MatchStatus.NOT_APPLICABLE: 0.0,
}

ELIGIBILITY_CREDIT: Dict[EligibilityStatus, float] = {
    EligibilityStatus.ELIGIBLE: 1.0,
    EligibilityStatus.LIKELY_ELIGIBLE: 0.8,
    EligibilityStatus.UNCERTAIN: 0.4,
    EligibilityStatus.INELIGIBLE: 0.0,
}

COMPONENT_BY_CATEGORY: Dict[RequirementCategory, str] = {
    RequirementCategory.TECHNICAL_SKILL: "technical",
    RequirementCategory.DOMAIN_KNOWLEDGE: "technical",
    RequirementCategory.CERTIFICATION: "education",
    RequirementCategory.EDUCATION: "education",
    RequirementCategory.EXPERIENCE: "experience",
}


@dataclass
class ScoreResult:
    fit_score: int
    component_scores: Dict[str, float]
    match_type: MatchType
    priority: str
    confidence: ConfidenceLevel
    strengths: List[str] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    uncertainties: List[str] = field(default_factory=list)
    # requirement id -> (weight, contribution) for persistence/audit.
    requirement_weights: Dict[str, tuple] = field(default_factory=dict)


class FitScoringEngine:
    """Combines requirement assessments, eligibility and preferences into a score."""

    def __init__(
        self,
        policy_weights: Optional[Dict[str, float]] = None,
        policy_version: str = POLICY_VERSION,
    ):
        weights = dict(policy_weights or DEFAULT_POLICY_WEIGHTS)
        total = sum(weights.values())
        if total <= 0:
            raise ValueError("Policy weights must sum to a positive number")
        # Normalize so any caller-provided policy still yields a 0-100 score.
        self.policy_weights = {key: value / total for key, value in weights.items()}
        self.policy_version = policy_version

    def score(
        self,
        assessments: List[RequirementAssessment],
        eligibility: EligibilityEvaluation,
        preferences: Optional[PreferenceAssessment] = None,
        job_context: Optional[Dict[str, int]] = None,
    ) -> ScoreResult:
        """Score one job.

        Args:
            assessments: Requirement assessments from the orchestrator.
            eligibility: Hard-gate outcome.
            preferences: Soft preference signals, if available.
            job_context: Optional facts about the job used to detect low
                coverage: ``description_length`` and ``technical_requirement_count``.
        """
        earned: Dict[str, float] = {key: 0.0 for key in self.policy_weights}
        possible: Dict[str, float] = {key: 0.0 for key in self.policy_weights}
        strengths: List[str] = []
        gaps: List[str] = []
        uncertainties: List[str] = []
        requirement_weights: Dict[str, tuple] = {}

        for assessment in assessments:
            component = COMPONENT_BY_CATEGORY.get(assessment.requirement.category)
            if component is None or component not in possible:
                # Location/preference/eligibility categories are scored by their
                # own dedicated components, not as generic requirements.
                continue

            weight = STRICTNESS_WEIGHT.get(assessment.requirement.strictness, 0.2)
            # A low-confidence extraction should not carry full weight.
            weight *= _confidence_factor(assessment.requirement.confidence)

            if assessment.status is MatchStatus.UNKNOWN:
                # Not counted for or against; surfaced instead.
                uncertainties.append(
                    f"{assessment.requirement.normalized_name}: {assessment.explanation}"
                )
                requirement_weights[assessment.requirement.id] = (round(weight, 3), 0.0)
                continue

            credit = STATUS_CREDIT.get(assessment.status, 0.0)
            contribution = weight * credit
            possible[component] += weight
            earned[component] += contribution
            requirement_weights[assessment.requirement.id] = (
                round(weight, 3),
                round(contribution, 3),
            )

            name = assessment.requirement.normalized_name
            if assessment.status is MatchStatus.MATCHED:
                strengths.append(name)
            elif assessment.status is MatchStatus.PARTIAL:
                strengths.append(f"{name} (partial)")
            elif assessment.status is MatchStatus.MISSING and assessment.requirement.strictness in (
                Strictness.HARD,
                Strictness.REQUIRED,
                Strictness.PREFERRED,
            ):
                gaps.append(name)

        component_scores: Dict[str, float] = {}
        for component in self.policy_weights:
            if component == "eligibility":
                component_scores[component] = ELIGIBILITY_CREDIT.get(eligibility.status, 0.0)
            elif component == "preferences":
                component_scores[component] = preferences.score if preferences else 0.5
            elif possible[component] > 0 and component == "technical":
                # Thin evidence (2026-09-14): "Python" and "SQL" matched made the
                # technical component 100%. A neutral prior worth one required
                # requirement keeps a two-keyword match from reading as complete.
                component_scores[component] = (earned[component] + 0.5 * TECHNICAL_EVIDENCE_PRIOR) / (possible[component] + TECHNICAL_EVIDENCE_PRIOR)
                if possible[component] < 3 * TECHNICAL_EVIDENCE_PRIOR:
                    uncertainties.append("Few technical requirements were read, so the technical score is weighted toward neutral.")
            elif possible[component] > 0:
                component_scores[component] = earned[component] / possible[component]
            else:
                # The JD expressed nothing in this dimension. Scoring it zero
                # would punish the job for the JD's brevity, so it is dropped
                # from the weighted average instead (see below).
                component_scores[component] = -1.0

        applicable = {k: v for k, v in component_scores.items() if v >= 0.0}
        weight_sum = sum(self.policy_weights[k] for k in applicable)
        if weight_sum > 0:
            raw = sum(self.policy_weights[k] * v for k, v in applicable.items()) / weight_sum
        else:
            raw = 0.0

        fit_score = int(round(raw * 100))

        # Hard ineligibility caps the score: a job the candidate cannot take
        # must never outrank one they can.
        if eligibility.status is EligibilityStatus.INELIGIBLE:
            fit_score = min(fit_score, 20)

        uncertainties.extend(eligibility.uncertainties)
        if preferences and preferences.excluded and preferences.exclusion_reason:
            gaps.append(preferences.exclusion_reason)

        # Low technical coverage: the JD had substantial prose but produced no
        # recognizable technical requirement. Dropping the component from the
        # average would score such a job purely on preferences + eligibility and
        # rank an unread job alongside a genuinely matched one. That is exactly
        # the "ambiguity presented as certainty" the PRD forbids, so the job is
        # routed to human review instead of being ranked as a confident match.
        context = job_context or {}
        description_length = context.get("description_length", 0)
        technical_requirements = context.get("technical_requirement_count", 0)
        # The floor only excludes descriptions too short to state anything at
        # all (an empty or one-line JD is "nothing was said", not "we failed to
        # read it"). Anything longer that yields no technical requirement is a
        # comprehension failure and must be surfaced as such.
        MIN_MEANINGFUL_DESCRIPTION = 60
        # Note: component_scores still carries the -1.0 "not applicable"
        # sentinel here, so absence must be tested by value, not by key.
        low_coverage = (
            component_scores.get("technical", -1.0) < 0.0
            and technical_requirements == 0
            and description_length >= MIN_MEANINGFUL_DESCRIPTION
        )
        if low_coverage:
            uncertainties.append(
                "No recognizable technical requirements were extracted from this job "
                "description, so the score reflects only preferences and eligibility."
            )

        # Selection quality (2026-09-14): role-family relevance and unread
        # descriptions cap fit. Perplexity "Motion Designer" scored 69 and Zeta
        # "Cloud Network Engineer II" 100 on employment type, location and
        # eligibility alone. Every cap is recorded so the score explains itself.
        role = getattr(preferences, "role", None) if preferences is not None else None
        caps = []
        if role is not None and role.relevance is Relevance.UNRELATED:
            caps.append((UNRELATED_ROLE_FIT_CAP, f"{role.detail}, so fit is capped at {UNRELATED_ROLE_FIT_CAP}."))
        elif role is not None and role.relevance is Relevance.WEAK:
            caps.append((WEAK_ROLE_FIT_CAP, f"{role.detail}, so fit is capped at {WEAK_ROLE_FIT_CAP}."))
        if low_coverage:
            caps.append((LOW_COVERAGE_FIT_CAP, f"No technical requirement was read, so fit is capped at {LOW_COVERAGE_FIT_CAP}."))
        for cap, note in sorted(caps):
            if fit_score > cap:
                uncertainties.append(note[0].upper() + note[1:])
                fit_score = cap

        confidence = _score_confidence(assessments, eligibility, uncertainties)
        priority = _priority(fit_score, eligibility.status)
        if low_coverage:
            confidence = ConfidenceLevel.LOW
            if priority in ("P0", "P1", "P2"):
                priority = "REVIEW"

        return ScoreResult(
            fit_score=fit_score,
            component_scores={
                k: round(v, 4) for k, v in component_scores.items() if v >= 0.0
            },
            match_type=_match_type(fit_score, eligibility.status),
            priority=priority,
            confidence=confidence,
            strengths=strengths,
            gaps=gaps,
            uncertainties=uncertainties,
            requirement_weights=requirement_weights,
        )


def _confidence_factor(level: ConfidenceLevel) -> float:
    return {
        ConfidenceLevel.HIGH: 1.0,
        ConfidenceLevel.MEDIUM: 0.75,
        ConfidenceLevel.LOW: 0.5,
        ConfidenceLevel.UNKNOWN: 0.5,
    }.get(level, 0.75)


def _match_type(fit_score: int, eligibility: EligibilityStatus) -> MatchType:
    if eligibility is EligibilityStatus.INELIGIBLE:
        return MatchType.POOR_MATCH
    if fit_score >= 75:
        return MatchType.CORE_MATCH
    if fit_score >= 55:
        return MatchType.STRETCH_MATCH
    if fit_score > 0:
        return MatchType.POOR_MATCH
    return MatchType.UNKNOWN


def _priority(fit_score: int, eligibility: EligibilityStatus) -> str:
    if eligibility is EligibilityStatus.INELIGIBLE:
        return "IGNORE"
    if eligibility is EligibilityStatus.UNCERTAIN and fit_score >= 55:
        # Worth a look, but a human should resolve the unknown first.
        return "REVIEW"
    if fit_score >= 85:
        return "P0"
    if fit_score >= 70:
        return "P1"
    if fit_score >= 55:
        return "P2"
    return "IGNORE"


def _score_confidence(
    assessments: List[RequirementAssessment],
    eligibility: EligibilityEvaluation,
    uncertainties: List[str],
) -> ConfidenceLevel:
    """Confidence in the score itself — never hardcoded."""
    if not assessments:
        return ConfidenceLevel.LOW
    if eligibility.confidence is ConfidenceLevel.LOW or len(uncertainties) >= 3:
        return ConfidenceLevel.LOW

    low = sum(1 for a in assessments if a.confidence is ConfidenceLevel.LOW)
    if low > len(assessments) / 2:
        return ConfidenceLevel.LOW
    if eligibility.confidence is ConfidenceLevel.MEDIUM or uncertainties:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.HIGH
