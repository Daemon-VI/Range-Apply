"""Human-readable explanations for a match decision.

FR-06 requires eligibility and relevance to expose *reasons*, not only scores.
Everything printed here is derived from the assessments and the eligibility
gates — no claim appears in an explanation that is not backed by a requirement
or a Career Brain reference.

Uncertainty is stated explicitly. A match that could not resolve a requirement
says so rather than presenting a confident-looking score.
"""

from typing import List, Optional

from app.intelligence.models.enums import (
    ConfidenceLevel,
    EligibilityStatus,
    MatchStatus,
    MatchType,
    Strictness,
)
from app.intelligence.models.requirements import RequirementAssessment
from app.intelligence.services.eligibility_engine import EligibilityEvaluation
from app.intelligence.services.preference_evaluator import PreferenceAssessment

_MAX_ITEMS = 6


class ExplanationGenerator:
    """Generates human-readable explanations for job match assessments."""

    def generate(
        self,
        match_type: MatchType,
        strengths: List[str],
        gaps: List[str],
        assessments: List[RequirementAssessment],
        eligibility: Optional[EligibilityEvaluation] = None,
        preferences: Optional[PreferenceAssessment] = None,
        uncertainties: Optional[List[str]] = None,
        fit_score: Optional[int] = None,
        confidence: Optional[ConfidenceLevel] = None,
    ) -> str:
        lines: List[str] = []

        # --- headline -------------------------------------------------
        headline = {
            MatchType.CORE_MATCH: "Strong match: the role's core requirements are backed by verified evidence.",
            MatchType.STRETCH_MATCH: "Stretch match: real evidence for most requirements, with some gaps.",
            MatchType.POOR_MATCH: "Weak match: key required capabilities are missing.",
            MatchType.UNKNOWN: "Not enough information to judge this role.",
        }.get(match_type, "Match assessed.")
        if fit_score is not None:
            headline = f"{headline} (fit {fit_score}/100)"
        lines.append(headline)

        # --- eligibility ----------------------------------------------
        if eligibility is not None:
            lines.append(f"Eligibility: {_eligibility_sentence(eligibility)}")
            for reason in eligibility.blocking_reasons[:_MAX_ITEMS]:
                lines.append(f"  - Blocker: {reason}")
            for reason in eligibility.uncertainties[:_MAX_ITEMS]:
                lines.append(f"  - Unresolved: {reason}")

        # --- strengths, backed by requirement evidence ----------------
        if strengths:
            lines.append(f"Strengths: {', '.join(strengths[:_MAX_ITEMS])}")
            for assessment in _top_evidence(assessments):
                lines.append(
                    f"  - {assessment.requirement.normalized_name}: {assessment.explanation}"
                )

        # --- gaps, hard ones first ------------------------------------
        if gaps:
            hard_gaps = [
                a.requirement.normalized_name
                for a in assessments
                if a.status is MatchStatus.MISSING
                and a.requirement.strictness in (Strictness.HARD, Strictness.REQUIRED)
            ]
            if hard_gaps:
                lines.append(f"Required but missing: {', '.join(hard_gaps[:_MAX_ITEMS])}")
            soft_gaps = [g for g in gaps if g not in hard_gaps]
            if soft_gaps:
                lines.append(f"Nice-to-have gaps: {', '.join(soft_gaps[:_MAX_ITEMS])}")

        # --- uncertainty ----------------------------------------------
        if uncertainties:
            lines.append(f"Unknowns ({len(uncertainties)}): {'; '.join(uncertainties[:3])}")

        # --- preferences ----------------------------------------------
        if preferences and preferences.applicable:
            if preferences.matches:
                lines.append(f"Preference fit: {'; '.join(preferences.matches[:3])}")
            if preferences.mismatches:
                lines.append(f"Preference friction: {'; '.join(preferences.mismatches[:3])}")

        # --- recommendation -------------------------------------------
        lines.append(f"Recommendation: {_recommendation(match_type, eligibility, confidence)}")
        return "\n".join(lines)


def _eligibility_sentence(evaluation: EligibilityEvaluation) -> str:
    label = {
        EligibilityStatus.ELIGIBLE: "eligible",
        EligibilityStatus.LIKELY_ELIGIBLE: "likely eligible (no hard constraints stated)",
        EligibilityStatus.UNCERTAIN: "uncertain - a stated constraint could not be resolved",
        EligibilityStatus.INELIGIBLE: "ineligible",
    }[evaluation.status]
    return f"{label} (confidence {evaluation.confidence.value.lower()})"


def _top_evidence(assessments: List[RequirementAssessment]) -> List[RequirementAssessment]:
    """The strongest matched requirements, for evidence lines."""
    matched = [
        a
        for a in assessments
        if a.status is MatchStatus.MATCHED
        and a.requirement.strictness in (Strictness.HARD, Strictness.REQUIRED)
        and a.evidence_references
    ]
    return matched[:3]


def _recommendation(
    match_type: MatchType,
    eligibility: Optional[EligibilityEvaluation],
    confidence: Optional[ConfidenceLevel],
) -> str:
    if eligibility is not None:
        if eligibility.status is EligibilityStatus.INELIGIBLE:
            return "Do not apply - a hard eligibility requirement is not met."
        if eligibility.status is EligibilityStatus.UNCERTAIN:
            return "Review manually - resolve the unresolved eligibility question before applying."

    if match_type is MatchType.CORE_MATCH:
        base = "Worth applying."
    elif match_type is MatchType.STRETCH_MATCH:
        base = "Worth applying if the gaps can be addressed honestly in the application."
    else:
        base = "Not recommended."

    if confidence is ConfidenceLevel.LOW:
        return f"{base} Treat this score as low confidence; the job description gave little to work with."
    return base
