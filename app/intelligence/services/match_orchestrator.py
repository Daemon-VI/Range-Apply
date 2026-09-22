"""Connects Phase 2 job discovery with the Phase 1 Career Brain to produce matches.

Pipeline for one job:

    NormalizedJob
      -> RequirementExtractor   (typed, traceable requirements)
      -> EligibilityEngine      (hard gates, UNCERTAIN preserved)
      -> EvidenceResolver       (Career Brain evidence per requirement)
      -> PreferenceEvaluator    (soft ranking signals)
      -> FitScoringEngine       (policy-weighted component scores)
      -> ExplanationGenerator   (reasons, not just numbers)

Everything is deterministic: two runs over the same job with the same Career
Brain and policy produce identical scores, which is what makes the match
reproducibility tests meaningful.
"""

import logging
import uuid
from typing import Optional

from app.core.timeutils import utc_now
from app.intelligence.extraction.requirement_extractor import RequirementExtractor
from app.intelligence.models.enums import (
    ConfidenceLevel,
    EvidenceStrength,
    MatchStatus,
    RequirementCategory,
    Strictness,
)
from app.intelligence.models.job_match import JobMatch, MatchRunInfo
from app.intelligence.models.requirements import RequirementAssessment
from app.intelligence.services.eligibility_engine import EligibilityEngine
from app.intelligence.services.evidence_resolver import EvidenceResolver
from app.intelligence.services.explanation_generator import ExplanationGenerator
from app.intelligence.services.fit_scoring_engine import (
    ENGINE_VERSION,
    FitScoringEngine,
    ScoreResult,
)
from app.intelligence.services.preference_evaluator import PreferenceAssessment, PreferenceEvaluator
from app.jobs.models.job import NormalizedJob

logger = logging.getLogger(__name__)

# Categories resolved against the candidate's skill vocabulary.
_SKILL_CATEGORIES = (
    RequirementCategory.TECHNICAL_SKILL,
    RequirementCategory.DOMAIN_KNOWLEDGE,
)

_IMPACT_BY_STRICTNESS = {
    Strictness.HARD: "critical",
    Strictness.REQUIRED: "high",
    Strictness.PREFERRED: "medium",
    Strictness.NICE_TO_HAVE: "low",
    Strictness.SIGNAL: "low",
    Strictness.UNKNOWN: "low",
}


class MatchOrchestrator:
    """Produces a full JobMatch for a normalized job."""

    def __init__(
        self,
        evidence_resolver: EvidenceResolver,
        req_interpreter=None,
        eligibility_engine: Optional[EligibilityEngine] = None,
        scoring_engine: Optional[FitScoringEngine] = None,
        explanation_gen: Optional[ExplanationGenerator] = None,
        requirement_extractor: Optional[RequirementExtractor] = None,
        preference_evaluator: Optional[PreferenceEvaluator] = None,
    ):
        self.evidence_resolver = evidence_resolver
        # Kept for backwards compatibility with the original constructor
        # signature; the richer RequirementExtractor is what actually runs.
        self.req_interpreter = req_interpreter
        self.eligibility_engine = eligibility_engine
        self.scoring_engine = scoring_engine or FitScoringEngine()
        self.explanation_gen = explanation_gen or ExplanationGenerator()
        self.requirement_extractor = requirement_extractor or RequirementExtractor()
        self.preference_evaluator = preference_evaluator or PreferenceEvaluator()

    def evaluate_job(self, job: NormalizedJob, run_id: Optional[str] = None) -> JobMatch:
        run_id = run_id or str(uuid.uuid4())

        # 1. Hard gates.
        eligibility = self.eligibility_engine.evaluate_detailed(job)

        # 2. Typed requirements from the JD.
        requirements = self.requirement_extractor.extract(job)

        # 3. Evidence per requirement.
        assessments = [self._assess(req) for req in requirements]

        # 4. Soft preference signals.
        preferences = self._preferences(job)

        # 5. Policy-weighted score. The job context lets the engine detect a
        # description it failed to understand, rather than scoring it as if the
        # missing dimension simply did not apply.
        scoring: ScoreResult = self.scoring_engine.score(
            assessments,
            eligibility,
            preferences,
            job_context={
                "description_length": len(job.description or ""),
                "technical_requirement_count": sum(
                    1 for r in requirements if r.category in _SKILL_CATEGORIES
                ),
            },
        )

        # Fold the per-requirement weights back into the assessments so the
        # persisted rows explain how each one moved the score.
        for assessment in assessments:
            weight, contribution = scoring.requirement_weights.get(
                assessment.requirement.id, (0.0, 0.0)
            )
            assessment.weight = weight
            assessment.contribution = contribution

        # 6. Explanation.
        explanation = self.explanation_gen.generate(
            match_type=scoring.match_type,
            strengths=scoring.strengths,
            gaps=scoring.gaps,
            assessments=assessments,
            eligibility=eligibility,
            preferences=preferences,
            uncertainties=scoring.uncertainties,
            fit_score=scoring.fit_score,
            confidence=scoring.confidence,
        )

        match_run_info = MatchRunInfo(
            run_id=run_id,
            job_version=job.content_hash or "unknown",
            career_brain_version="v1",
            policy_version=self.scoring_engine.policy_version,
            engine_version=ENGINE_VERSION,
            timestamp=utc_now(),
        )

        return JobMatch(
            id=str(uuid.uuid4()),
            job_id=job.id or str(uuid.uuid4()),
            job_canonical_key=job.canonical_key,
            match_run=match_run_info,
            eligibility=eligibility.to_result(),
            fit_score=scoring.fit_score,
            priority=scoring.priority,
            match_type=scoring.match_type,
            confidence=scoring.confidence,
            requirement_assessments=assessments,
            strengths=scoring.strengths,
            gaps=scoring.gaps,
            explanation=explanation,
            generated_metadata={
                "component_scores": scoring.component_scores,
                "uncertainties": scoring.uncertainties,
                "eligibility_confidence": eligibility.confidence.value,
                "requirement_count": len(requirements),
                "job_content_hash": job.content_hash,
            },
        )

    # ---------------------------------------------------------------- #

    def _preferences(self, job: NormalizedJob) -> Optional[PreferenceAssessment]:
        try:
            preference_model = self.evidence_resolver.resolve_preferences()
        except Exception:  # noqa: BLE001 - preferences are optional context
            logger.exception("Could not load preferences; scoring without them")
            return None
        try:
            profile_location = self.evidence_resolver.career_brain.get_profile().location
        except Exception:  # noqa: BLE001 - the location target is optional context too
            profile_location = None
        return self.preference_evaluator.evaluate(job, preference_model, profile_location=profile_location)

    def _assess(self, requirement) -> RequirementAssessment:
        """Resolve one requirement into a typed assessment."""
        if requirement.category in _SKILL_CATEGORIES:
            evidence = self.evidence_resolver.resolve_skill(requirement.normalized_name)
            status = _status_from_strength(evidence.strength)
            confidence = _assessment_confidence(requirement.confidence, evidence.method)
            return RequirementAssessment(
                requirement=requirement,
                status=status,
                evidence_strength=evidence.strength,
                evidence_references=evidence.references,
                confidence=confidence,
                impact=_IMPACT_BY_STRICTNESS.get(requirement.strictness, "low"),
                explanation=evidence.description,
            )

        if requirement.category is RequirementCategory.EXPERIENCE:
            return self._assess_experience(requirement)

        if requirement.category is RequirementCategory.EDUCATION:
            return self._assess_education(requirement)

        # Location, eligibility, work authorization and preference categories
        # are decided by the eligibility engine and preference evaluator, not
        # by evidence lookup. They are recorded as NOT_APPLICABLE here so the
        # requirement still appears in the audit trail.
        return RequirementAssessment(
            requirement=requirement,
            status=MatchStatus.NOT_APPLICABLE,
            evidence_strength=EvidenceStrength.NONE,
            evidence_references=[],
            confidence=requirement.confidence,
            impact=_IMPACT_BY_STRICTNESS.get(requirement.strictness, "low"),
            explanation=f"Handled by the {requirement.category.value.lower()} gate, not by skill evidence.",
        )

    def _assess_experience(self, requirement) -> RequirementAssessment:
        """Years-of-experience gates, honest about unknowns."""
        required_months = requirement.experience_duration_months
        if required_months is None:
            # A seniority signal like "MID" rather than a duration.
            return RequirementAssessment(
                requirement=requirement,
                status=MatchStatus.UNKNOWN,
                evidence_strength=EvidenceStrength.NONE,
                evidence_references=[],
                confidence=ConfidenceLevel.LOW,
                impact=_IMPACT_BY_STRICTNESS.get(requirement.strictness, "low"),
                explanation="Seniority signal with no measurable duration requirement.",
            )

        actual_months = self.evidence_resolver.total_experience_months()
        if actual_months is None:
            return RequirementAssessment(
                requirement=requirement,
                status=MatchStatus.UNKNOWN,
                evidence_strength=EvidenceStrength.NONE,
                evidence_references=[],
                confidence=ConfidenceLevel.LOW,
                impact=_IMPACT_BY_STRICTNESS.get(requirement.strictness, "low"),
                explanation=(
                    f"Job asks for {required_months // 12} year(s) of experience; the Career "
                    "Brain records no dated employment to measure against."
                ),
            )

        if actual_months >= required_months:
            status, strength = MatchStatus.MATCHED, EvidenceStrength.DIRECT_VERIFIED
        elif actual_months >= required_months * 0.5:
            status, strength = MatchStatus.PARTIAL, EvidenceStrength.SUPPORTING
        else:
            status, strength = MatchStatus.MISSING, EvidenceStrength.NONE

        return RequirementAssessment(
            requirement=requirement,
            status=status,
            evidence_strength=strength,
            evidence_references=[],
            confidence=ConfidenceLevel.MEDIUM,
            impact=_IMPACT_BY_STRICTNESS.get(requirement.strictness, "low"),
            explanation=(
                f"Requires ~{required_months} months of experience; "
                f"Career Brain records ~{actual_months} months."
            ),
        )

    def _assess_education(self, requirement) -> RequirementAssessment:
        """Degree-level requirements against the recorded degree."""
        profile = self.evidence_resolver.career_brain.get_profile()
        degree_text = f"{profile.degree} {profile.branch}".lower()
        name = requirement.normalized_name.lower()

        required_level = next(
            (level for level in ("phd", "master", "bachelor", "associate") if level in name), None
        )
        if required_level is None:
            return RequirementAssessment(
                requirement=requirement,
                status=MatchStatus.UNKNOWN,
                evidence_strength=EvidenceStrength.NONE,
                evidence_references=[],
                confidence=ConfidenceLevel.LOW,
                impact=_IMPACT_BY_STRICTNESS.get(requirement.strictness, "low"),
                explanation=f"Could not determine the degree level from '{requirement.normalized_name}'.",
            )

        held = "bachelor" if any(k in degree_text for k in ("b.tech", "btech", "bachelor", "b.e", "bsc")) else None
        if "m.tech" in degree_text or "master" in degree_text:
            held = "master"
        if "phd" in degree_text or "doctor" in degree_text:
            held = "phd"

        rank = {"associate": 1, "bachelor": 2, "master": 3, "phd": 4}
        if held is None:
            status, strength = MatchStatus.UNKNOWN, EvidenceStrength.NONE
            explanation = f"Recorded degree '{profile.degree}' could not be ranked against the requirement."
        elif rank[held] >= rank[required_level]:
            status, strength = MatchStatus.MATCHED, EvidenceStrength.DIRECT_VERIFIED
            explanation = f"Candidate holds a {held}'s degree ({profile.degree})."
        else:
            status, strength = MatchStatus.MISSING, EvidenceStrength.NONE
            explanation = (
                f"Requires a {required_level}'s degree; candidate holds {profile.degree}."
            )

        return RequirementAssessment(
            requirement=requirement,
            status=status,
            evidence_strength=strength,
            evidence_references=[],
            confidence=ConfidenceLevel.MEDIUM,
            impact=_IMPACT_BY_STRICTNESS.get(requirement.strictness, "low"),
            explanation=explanation,
        )


def _status_from_strength(strength: EvidenceStrength) -> MatchStatus:
    if strength in (EvidenceStrength.DIRECT_VERIFIED, EvidenceStrength.STRONG_DEMONSTRATED):
        return MatchStatus.MATCHED
    if strength in (
        EvidenceStrength.SUPPORTING,
        EvidenceStrength.INDIRECT,
        EvidenceStrength.ADJACENT,
    ):
        return MatchStatus.PARTIAL
    return MatchStatus.MISSING


def _assessment_confidence(requirement_confidence: ConfidenceLevel, method: str) -> ConfidenceLevel:
    """Confidence combines how the requirement was read and how it was matched."""
    method_confidence = {
        "exact": ConfidenceLevel.HIGH,
        "alias": ConfidenceLevel.HIGH,
        "lexical": ConfidenceLevel.MEDIUM,
        "related": ConfidenceLevel.LOW,
        "semantic": ConfidenceLevel.LOW,
        "none": ConfidenceLevel.MEDIUM,
    }.get(method, ConfidenceLevel.MEDIUM)

    order = [ConfidenceLevel.LOW, ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH]
    if requirement_confidence not in order:
        return method_confidence
    return order[min(order.index(requirement_confidence), order.index(method_confidence))]
