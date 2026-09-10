"""Hard-gate eligibility evaluation.

Separation of concerns (PRD P3): this module answers *"can the candidate apply
at all?"*. Soft desirability lives in the preference evaluator and the scoring
engine — a job the candidate merely dislikes is not ineligible.

Three rules the previous implementation broke:

* A requirement expressing only a **maximum** graduation year ("graduating by
  2028") fell through an ``elif`` chain and was never evaluated, so an
  ineligible candidate was reported ELIGIBLE.
* Every result was reported with hardcoded HIGH confidence.
* There was no UNCERTAIN outcome, so "the JD does not say" and "the candidate
  qualifies" were indistinguishable.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from app.intelligence.models.enums import ConfidenceLevel, EligibilityStatus
from app.intelligence.models.job_match import EligibilityResult
from app.jobs.models.job import GraduationRequirement, NormalizedJob
from app.models.profile import Profile
from app.services.career_brain import CareerBrainService

logger = logging.getLogger(__name__)


@dataclass
class GateOutcome:
    """Result of a single hard gate."""

    name: str
    status: EligibilityStatus
    reason: str
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH


@dataclass
class EligibilityEvaluation:
    """Full eligibility picture, including what could not be determined."""

    status: EligibilityStatus
    confidence: ConfidenceLevel
    eligibility_reasons: List[str] = field(default_factory=list)
    blocking_reasons: List[str] = field(default_factory=list)
    uncertainties: List[str] = field(default_factory=list)
    gates: List[GateOutcome] = field(default_factory=list)

    def to_result(self) -> EligibilityResult:
        return EligibilityResult(
            status=self.status,
            eligibility_reasons=list(self.eligibility_reasons),
            blocking_reasons=list(self.blocking_reasons),
        )


# Phrases that indicate the candidate needs sponsorship or is unauthorized.
_NEEDS_SPONSORSHIP_HINTS = ("require sponsorship", "need sponsorship", "not authorized", "no work authorization")
_AUTHORIZED_HINTS = ("citizen", "permanent resident", "green card", "authorized", "no sponsorship required")


class EligibilityEngine:
    """Evaluates hard-gate eligibility rules against the Career Brain."""

    def __init__(self, career_brain: CareerBrainService):
        self.career_brain = career_brain

    def evaluate(self, job: NormalizedJob) -> EligibilityResult:
        """Backwards-compatible entry point returning the simple result model."""
        return self.evaluate_detailed(job).to_result()

    def evaluate_detailed(self, job: NormalizedJob) -> EligibilityEvaluation:
        """Run every hard gate and combine them conservatively."""
        profile = self.career_brain.get_profile()

        gates: List[GateOutcome] = []
        graduation_gate = self._graduation_gate(job, profile)
        if graduation_gate:
            gates.append(graduation_gate)

        work_auth_gate = self._work_authorization_gate(job, profile)
        if work_auth_gate:
            gates.append(work_auth_gate)

        location_gate = self._location_gate(job, profile)
        if location_gate:
            gates.append(location_gate)

        return self._combine(gates)

    # ---------------------------------------------------------------- #

    def _combine(self, gates: List[GateOutcome]) -> EligibilityEvaluation:
        """Worst outcome wins; unknowns stay visible rather than being rounded up."""
        eligibility_reasons = [g.reason for g in gates if g.status is EligibilityStatus.ELIGIBLE]
        blocking_reasons = [g.reason for g in gates if g.status is EligibilityStatus.INELIGIBLE]
        uncertainties = [g.reason for g in gates if g.status is EligibilityStatus.UNCERTAIN]

        if blocking_reasons:
            status = EligibilityStatus.INELIGIBLE
        elif uncertainties:
            status = EligibilityStatus.UNCERTAIN
        elif eligibility_reasons:
            status = EligibilityStatus.ELIGIBLE
        else:
            # No hard gate was expressed by the JD at all. That is not the same
            # as passing one, so the honest answer is "probably fine, unproven".
            status = EligibilityStatus.LIKELY_ELIGIBLE
            eligibility_reasons.append(
                "No hard eligibility constraints were detected in this job description."
            )

        confidence = self._confidence(gates, status)
        return EligibilityEvaluation(
            status=status,
            confidence=confidence,
            eligibility_reasons=eligibility_reasons,
            blocking_reasons=blocking_reasons,
            uncertainties=uncertainties,
            gates=gates,
        )

    @staticmethod
    def _confidence(gates: List[GateOutcome], status: EligibilityStatus) -> ConfidenceLevel:
        """Derived from the evidence, never hardcoded."""
        if not gates:
            # Nothing to evaluate: we are confident only that nothing was stated.
            return ConfidenceLevel.LOW
        if status is EligibilityStatus.UNCERTAIN:
            return ConfidenceLevel.LOW
        relevant = [
            g
            for g in gates
            if g.status is (
                EligibilityStatus.INELIGIBLE
                if status is EligibilityStatus.INELIGIBLE
                else EligibilityStatus.ELIGIBLE
            )
        ]
        levels = [g.confidence for g in relevant] or [g.confidence for g in gates]
        if any(level is ConfidenceLevel.LOW for level in levels):
            return ConfidenceLevel.LOW
        if any(level is ConfidenceLevel.MEDIUM for level in levels):
            return ConfidenceLevel.MEDIUM
        return ConfidenceLevel.HIGH

    # ---------------------------------------------------------------- #

    def _graduation_gate(self, job: NormalizedJob, profile: Profile) -> Optional[GateOutcome]:
        """Evaluate exact years, minimum, maximum and ranges — all of them."""
        requirement: Optional[GraduationRequirement] = job.graduation_requirement
        single_year = job.graduation_year_requirement

        if requirement is None and single_year is None:
            return None

        candidate_year = profile.graduation_year
        if candidate_year is None:
            return GateOutcome(
                name="graduation",
                status=EligibilityStatus.UNCERTAIN,
                reason="Job states a graduation requirement but the profile has no graduation year.",
                confidence=ConfidenceLevel.LOW,
            )

        confidence = ConfidenceLevel.HIGH
        if requirement is not None:
            confidence = _confidence_from_score(requirement.extraction_confidence)

        # An explicitly extracted requirement takes priority over the scalar
        # convenience field, which is only a denormalized copy of it.
        if requirement is not None and (
            requirement.exact_years or requirement.minimum_year or requirement.maximum_year
        ):
            return self._evaluate_graduation_requirement(requirement, candidate_year, confidence)

        if single_year is not None:
            if candidate_year == single_year:
                return GateOutcome(
                    "graduation",
                    EligibilityStatus.ELIGIBLE,
                    f"Graduation year {candidate_year} matches the required {single_year}.",
                    confidence,
                )
            return GateOutcome(
                "graduation",
                EligibilityStatus.INELIGIBLE,
                f"Job requires graduation year {single_year}; candidate graduates {candidate_year}.",
                confidence,
            )

        # A requirement object exists but carries no usable years.
        return GateOutcome(
            "graduation",
            EligibilityStatus.UNCERTAIN,
            (
                "A graduation constraint was detected but could not be parsed into years: "
                f"'{(requirement.original_text if requirement else '')[:120]}'."
            ),
            ConfidenceLevel.LOW,
        )

    @staticmethod
    def _evaluate_graduation_requirement(
        requirement: GraduationRequirement, candidate_year: int, confidence: ConfidenceLevel
    ) -> GateOutcome:
        """Exact years / min / max / range, each evaluated on its own merits."""
        if requirement.exact_years:
            if candidate_year in requirement.exact_years:
                return GateOutcome(
                    "graduation",
                    EligibilityStatus.ELIGIBLE,
                    f"Graduation year {candidate_year} is in the accepted set "
                    f"{requirement.exact_years}.",
                    confidence,
                )
            return GateOutcome(
                "graduation",
                EligibilityStatus.INELIGIBLE,
                f"Job accepts graduation years {requirement.exact_years}; "
                f"candidate graduates {candidate_year}.",
                confidence,
            )

        minimum = requirement.minimum_year
        maximum = requirement.maximum_year

        # Both bounds are checked independently; neither can be skipped.
        if minimum is not None and candidate_year < minimum:
            return GateOutcome(
                "graduation",
                EligibilityStatus.INELIGIBLE,
                f"Job requires graduation in {minimum} or later; "
                f"candidate graduates {candidate_year}.",
                confidence,
            )
        if maximum is not None and candidate_year > maximum:
            return GateOutcome(
                "graduation",
                EligibilityStatus.INELIGIBLE,
                f"Job requires graduation by {maximum}; candidate graduates {candidate_year}.",
                confidence,
            )

        bounds = []
        if minimum is not None:
            bounds.append(f"from {minimum}")
        if maximum is not None:
            bounds.append(f"until {maximum}")
        return GateOutcome(
            "graduation",
            EligibilityStatus.ELIGIBLE,
            f"Graduation year {candidate_year} satisfies the requirement ({' '.join(bounds)}).",
            confidence,
        )

    def _work_authorization_gate(
        self, job: NormalizedJob, profile: Profile
    ) -> Optional[GateOutcome]:
        """Only fires when the JD actually raises an authorization constraint."""
        text = f"{job.description} {job.original_description}".lower()
        if not text.strip():
            return None

        blocking_phrases = (
            "not able to sponsor",
            "does not offer sponsorship",
            "do not offer sponsorship",
            "no visa sponsorship",
            "unable to sponsor",
            "without sponsorship",
            "must be a us citizen",
            "must be a u.s. citizen",
            "security clearance",
        )
        mentions_block = any(phrase in text for phrase in blocking_phrases)
        mentions_requirement = "authorized to work" in text or "work authorization" in text

        if not mentions_block and not mentions_requirement:
            return None

        stated = (profile.work_authorization or "").strip().lower()
        if not stated:
            return GateOutcome(
                name="work_authorization",
                status=EligibilityStatus.UNCERTAIN,
                reason=(
                    "Job states a work-authorization constraint but the profile does not "
                    "record the candidate's authorization status."
                ),
                confidence=ConfidenceLevel.LOW,
            )

        if any(hint in stated for hint in _NEEDS_SPONSORSHIP_HINTS) and mentions_block:
            return GateOutcome(
                "work_authorization",
                EligibilityStatus.INELIGIBLE,
                "Job does not sponsor and the candidate requires sponsorship.",
                ConfidenceLevel.MEDIUM,
            )

        if any(hint in stated for hint in _AUTHORIZED_HINTS):
            return GateOutcome(
                "work_authorization",
                EligibilityStatus.ELIGIBLE,
                "Candidate's recorded work authorization satisfies the stated constraint.",
                ConfidenceLevel.MEDIUM,
            )

        return GateOutcome(
            "work_authorization",
            EligibilityStatus.UNCERTAIN,
            (
                "Job states a work-authorization constraint that could not be matched "
                f"against the recorded status '{profile.work_authorization}'."
            ),
            ConfidenceLevel.LOW,
        )

    def _location_gate(self, job: NormalizedJob, profile: Profile) -> Optional[GateOutcome]:
        """Location is a hard gate only for explicitly on-site roles.

        Remote and hybrid roles are handled as *preferences*, not gates.
        """
        from app.jobs.models.enums import RemoteType  # local import avoids a cycle

        if job.remote_type is not RemoteType.ON_SITE or not job.location:
            return None

        candidate_location = (profile.location or "").strip()
        if not candidate_location:
            return GateOutcome(
                "location",
                EligibilityStatus.UNCERTAIN,
                f"On-site role in {job.location} but the profile records no location.",
                ConfidenceLevel.LOW,
            )

        job_tokens = _location_tokens(job.location)
        candidate_tokens = _location_tokens(candidate_location)
        if job_tokens & candidate_tokens:
            return GateOutcome(
                "location",
                EligibilityStatus.ELIGIBLE,
                f"On-site location {job.location} overlaps the candidate's location "
                f"{candidate_location}.",
                ConfidenceLevel.MEDIUM,
            )

        return GateOutcome(
            "location",
            EligibilityStatus.UNCERTAIN,
            (
                f"On-site role in {job.location}; the candidate is in {candidate_location}. "
                "Relocation willingness is not recorded, so this is not treated as a block."
            ),
            ConfidenceLevel.LOW,
        )


def _location_tokens(value: str) -> set:
    """Comparable location tokens ('Hyderabad, India' -> {hyderabad, india})."""
    import re

    return {
        token
        for token in re.split(r"[,/|]|\s+-\s+", value.lower())
        for token in [token.strip()]
        if len(token) > 2
    }


def _confidence_from_score(score: float) -> ConfidenceLevel:
    if score >= 0.9:
        return ConfidenceLevel.HIGH
    if score >= 0.7:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW
