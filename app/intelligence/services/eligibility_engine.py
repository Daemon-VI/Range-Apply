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
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, List, Optional

from app.intelligence.extraction.seniority import experience_statements, title_seniority
from app.intelligence.models.enums import ConfidenceLevel, EligibilityStatus
from app.intelligence.models.job_match import EligibilityResult
from app.intelligence.services.evidence_resolver import _months_between
from app.jobs.extraction.deterministic import extract_experience_level
from app.jobs.geography import (
    GeoClass,
    GeographyPolicy,
    authorization_countries,
    job_places,
    parse_target,
    required_work_countries,
)
from app.jobs.models.enums import ExperienceLevel
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


# Audit fix (2026-09-14): the recorded status is free text and the hints were
# plain substrings, so negations read as their opposite: "Not a US citizen"
# satisfied a US-citizens-only role, "Non-citizen, no work authorization in
# India" satisfied "authorized to work in India", and "I do not require
# sponsorship" read as needing it. Negated statements are recognised first and
# removed before any positive hint is read.
#: The candidate needs sponsorship or is not authorized.
_NEEDS_SPONSORSHIP_RE = re.compile(
    r"\b(?:requires?|required|needs?|will need|will require)\b(?:\s+[\w-]+){0,2}\s+sponsorship\b"
    r"|\bnot authori[sz]ed\b|\bunauthori[sz]ed\b|\bno work authori[sz]ation\b",
    re.IGNORECASE,
)
#: "I do not require sponsorship", "no sponsorship needed", "sponsorship is not required".
_SPONSORSHIP_NEGATED_RE = re.compile(
    r"\b(?:not|no|never|don'?t|doesn'?t|won'?t)\b(?:\s+[\w-]+){0,3}\s+sponsorship\b|\bsponsorship\s+(?:is\s+)?not\s+(?:required|needed)\b",
    re.IGNORECASE,
)
#: A negated citizenship / authorization clause: "not a US citizen", "non-citizen", "no work authorization".
_NEGATED_STATUS_RE = re.compile(
    r"\b(?:not|non|no|never|without)\b[\s-]+(?:an?\s+|the\s+)?(?:[\w.]+\s+){0,2}?(?:citizen(?:ship)?|authori[sz]ed|authori[sz]ation|permanent resident|green card)\b"
    r"|\bunauthori[sz]ed\b",
    re.IGNORECASE,
)
_AUTHORIZED_HINTS = ("citizen", "permanent resident", "green card", "authorized", "authorised", "no sponsorship required")
#: A security clearance requirement ("active Secret clearance", "able to obtain a security clearance").
_CLEARANCE_RE = re.compile(r"\b(?:security|secret|top secret|ts/sci|government)\s+clearance\b", re.IGNORECASE)
_CLEARANCE_NOT_REQUIRED_RE = re.compile(r"\b(?:no|not|without)\b[^.;]{0,30}\bclearance\b|\bclearance\b[^.;]{0,20}\bnot (?:required|needed)\b", re.IGNORECASE)


def _needs_sponsorship(stated: str) -> bool:
    return bool(_NEEDS_SPONSORSHIP_RE.search(stated)) and not (
        _SPONSORSHIP_NEGATED_RE.search(stated) and not re.search(r"\bnot authori[sz]ed\b|\bunauthori[sz]ed\b|\bno work authori[sz]ation\b", stated, re.IGNORECASE)
    )


def _affirmed(stated: str) -> str:
    """The recorded status with every negated citizenship / authorization clause removed."""
    return _NEGATED_STATUS_RE.sub(" ", stated)
#: A United States citizenship requirement, including the "(US Citizen)" title suffix real boards use.
_US_CITIZENSHIP_RE = re.compile(r"\bu\.?\s?s\.?\s+citizen(?:ship)?\b", re.IGNORECASE)
#: Below this much dated employment, a not-yet-graduated candidate is still pre-career.
PRE_CAREER_MAX_EMPLOYMENT_MONTHS = 12
_US_STATED_RE = re.compile(r"\bu\.?\s?s\.?\b|united states|american", re.IGNORECASE)


class EligibilityEngine:
    """Evaluates hard-gate eligibility rules against the Career Brain."""

    def __init__(self, career_brain: CareerBrainService, today: Optional[Callable[[], date]] = None):
        self.career_brain = career_brain
        self._today = today or date.today

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

        experience_gate = self._experience_gate(job, profile)
        if experience_gate:
            gates.append(experience_gate)

        return self._combine(gates)

    # ---------------------------------------------------------------- #

    def _pre_career(self, profile: Profile) -> bool:
        """A candidate who has not graduated yet and has less than a year of dated employment.

        Only what the Career Brain records counts: an unknown graduation year
        or an unreadable experience list never makes a candidate "pre-career".
        """
        graduation_year = getattr(profile, "graduation_year", None)
        if graduation_year is None or graduation_year < self._today().year:
            return False
        try:
            experience = self.career_brain.get_experience()
        except AttributeError:
            return False
        employment = [item for item in (experience or []) if getattr(item, "is_employment", False)]
        if not employment:
            return True
        # A short internship is employment but not a career: the real profile
        # (2026-09-13) has a 4-month internship, which switched the gate off and
        # made senior roles LIKELY again. Undated employment cannot be measured
        # and is never assumed to be short.
        months = 0
        for item in employment:
            measured = _months_between(getattr(item, "start_date", None), getattr(item, "end_date", None))
            if measured is None:
                return False
            months += measured
        return months < PRE_CAREER_MAX_EMPLOYMENT_MONTHS

    def _experience_gate(self, job: NormalizedJob, profile: Profile) -> Optional[GateOutcome]:
        """Seniority and years of experience against a student with no employment.

        Pilot finding (2026-09-13, real boards): with only graduation,
        authorization and location gated, "Senior Software Engineer" and
        "4+ years of professional experience" roles were LIKELY_ELIGIBLE for a
        third-year student, and admission chose them over the internships the
        candidate fits.

        Selection quality (2026-09-14): a senior title (including "Architect",
        "III" and "Staff") blocks; a stated requirement of 2+ years in the
        qualifications, 3+ years tied to the word experience anywhere, or a
        5+ year band ("(9 - 12 Years)", "5+ years in ML systems") blocks; a
        preferred or smaller mention, a mid-level title, or a level read only
        from the description stays UNCERTAIN. Every outcome quotes its text.
        """
        if not self._pre_career(profile):
            return None
        who = (
            f"the Career Brain records the candidate as a student graduating {profile.graduation_year} "
            "with less than a year of recorded employment"
        )
        # The stored level mixes the title with a bare "5+ years" anywhere in the
        # description (company age included); only its title part is used here,
        # and the description is read through experience_statements below.
        title_level = title_seniority(job.title or job.original_title)
        stored_title_level = extract_experience_level(job.title or job.original_title or "", "")
        if stored_title_level is ExperienceLevel.SENIOR or title_level == "SENIOR":
            return GateOutcome("experience", EligibilityStatus.INELIGIBLE, f"Senior-level role ('{job.title}'); {who}.", ConfidenceLevel.MEDIUM)
        in_qualifications = [s for s in experience_statements(job.qualifications or []) if not s.preferred]
        required = max(in_qualifications, key=lambda s: s.years, default=None)
        if required is not None and required.years >= 2:
            return GateOutcome(
                "experience",
                EligibilityStatus.INELIGIBLE,
                f"Job requires {required.years}+ years of experience ('{required.text}'); {who}.",
                ConfidenceLevel.MEDIUM,
            )
        statements = experience_statements([job.title, *(job.qualifications or []), job.description, job.original_description])
        # Selection quality (2026-09-14): a stated requirement anywhere in the
        # posting is a requirement ("Associate Architect (9 - 12 Years)" was
        # ELIGIBLE). A band read without the word "experience" blocks from 5
        # years; anything marked preferred, or smaller, stays UNCERTAIN.
        blocking = max((s for s in statements if not s.preferred and s.years >= (5 if s.loose else 3)), key=lambda s: s.years, default=None)
        if blocking is not None:
            return GateOutcome(
                "experience",
                EligibilityStatus.INELIGIBLE,
                f"Job asks for {blocking.years}+ years of experience ('{blocking.text}'); {who}.",
                ConfidenceLevel.MEDIUM,
            )
        mentioned = max(statements, key=lambda s: s.years, default=None)
        if mentioned is not None:
            return GateOutcome(
                "experience",
                EligibilityStatus.UNCERTAIN,
                f"Job mentions {mentioned.years}+ years of experience ('{mentioned.text}'{', preferred' if mentioned.preferred else ''}); {who}.",
                ConfidenceLevel.LOW,
            )
        if job.experience_level in (ExperienceLevel.MID, ExperienceLevel.SENIOR) or title_level == "MID":
            level = "Mid-level" if title_level == "MID" or job.experience_level is ExperienceLevel.MID else "Senior level read from the description, not confirmed by the title or a stated requirement, for this"
            return GateOutcome("experience", EligibilityStatus.UNCERTAIN, f"{level} role ('{job.title}'); {who}.", ConfidenceLevel.LOW)
        return None

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
        us_citizenship = _US_CITIZENSHIP_RE.search(f"{job.title} {job.original_title} {text}")
        if us_citizenship:
            return self._us_citizenship_gate(profile)
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
        )
        mentions_block = any(phrase in text for phrase in blocking_phrases)
        mentions_requirement = "authorized to work" in text or "work authorization" in text
        # Audit fix (2026-09-14): a clearance was a "blocking phrase" that any
        # recorded citizenship satisfied ("Indian citizen" -> ELIGIBLE for an
        # active Secret clearance). The Career Brain records no clearance.
        clearance = bool(_CLEARANCE_RE.search(text)) and not _CLEARANCE_NOT_REQUIRED_RE.search(text)

        if not mentions_block and not mentions_requirement and not clearance:
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

        if _needs_sponsorship(stated) and mentions_block:
            return GateOutcome(
                "work_authorization",
                EligibilityStatus.INELIGIBLE,
                "Job does not sponsor and the candidate requires sponsorship.",
                ConfidenceLevel.MEDIUM,
            )

        if clearance:
            return GateOutcome(
                "work_authorization",
                EligibilityStatus.UNCERTAIN,
                "Job requires a security clearance; the Career Brain records no clearance, and citizenship is not assumed to grant one.",
                ConfidenceLevel.LOW,
            )

        # "Indian citizen" is not authorization to work in the United States: the
        # "citizen" hint alone used to satisfy any country's requirement.
        affirmed = _affirmed(stated)
        required = required_work_countries(f"{job.description} {job.original_description}")
        recorded = authorization_countries(affirmed)
        if required and not required & recorded and (recorded or affirmed != stated):
            names = f"names only {', '.join(sorted(recorded))}" if recorded else "does not affirm authorization there"
            return GateOutcome(
                "work_authorization",
                EligibilityStatus.UNCERTAIN,
                f"Job requires authorization to work in {', '.join(sorted(required))}; the recorded status '{profile.work_authorization}' {names}.",
                ConfidenceLevel.LOW,
            )

        if not _needs_sponsorship(stated) and any(hint in affirmed for hint in _AUTHORIZED_HINTS):
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

    @staticmethod
    def _us_citizenship_gate(profile: Profile) -> GateOutcome:
        """A role limited to US citizens ("(US Citizen)" in the title, pilot finding).

        The recorded authorization decides; nothing is inferred from location.
        """
        stated = (profile.work_authorization or "").strip()
        if not stated:
            return GateOutcome(
                "work_authorization",
                EligibilityStatus.UNCERTAIN,
                "Role is limited to US citizens; the profile does not record the candidate's citizenship or authorization.",
                ConfidenceLevel.LOW,
            )
        affirmed = _affirmed(stated)
        if "citizen" in affirmed.lower() and _US_STATED_RE.search(affirmed):
            return GateOutcome("work_authorization", EligibilityStatus.ELIGIBLE, "Role is limited to US citizens; the candidate records US citizenship.", ConfidenceLevel.MEDIUM)
        negated_citizenship = re.search(r"\b(?:not|non|no|never)\b[\s-]+(?:an?\s+)?(?:u\.?\s?s\.?\s+|american\s+)?citizen", stated, re.IGNORECASE)
        if "citizen" in affirmed.lower() or negated_citizenship:
            return GateOutcome(
                "work_authorization",
                EligibilityStatus.INELIGIBLE,
                f"Role is limited to US citizens; the candidate records '{stated}'.",
                ConfidenceLevel.MEDIUM,
            )
        return GateOutcome(
            "work_authorization",
            EligibilityStatus.UNCERTAIN,
            f"Role is limited to US citizens; the recorded status '{stated}' does not state citizenship.",
            ConfidenceLevel.LOW,
        )

    def _location_gate(self, job: NormalizedJob, profile: Profile) -> Optional[GateOutcome]:
        """Where the work is, against where the candidate records being.

        Geographic targeting fix (2026-09-14): this gate only fired for postings
        extracted as ON_SITE, so hybrid, unknown-mode and "US Remote" roles
        passed as LIKELY for a candidate in Hyderabad. It now reads every
        location a posting carries (``app/jobs/geography.py``) for every work
        mode. It uses the candidate's *recorded location* only — the job-market
        preference is admission's GEOGRAPHY gate — and never infers work
        authorization, citizenship or relocation willingness from a city.
        Remote roles that state no country, and postings with no usable
        location, express no constraint and are not gated here.
        """
        candidate_location = (profile.location or "").strip()
        home = parse_target(candidate_location)
        if home is None or home.country is None:
            return self._unparsed_location_gate(job, candidate_location)

        places = job_places(job.location, job.locations, job.metadata)
        geo = GeographyPolicy(target=home).classify(places)
        where = job.location or ", ".join(job.locations or []) or "the stated location"
        if geo is GeoClass.PRIMARY:
            return GateOutcome("location", EligibilityStatus.ELIGIBLE, f"Job location {where} is in the candidate's area ({candidate_location}).", ConfidenceLevel.MEDIUM)
        if geo is GeoClass.COUNTRY_REMOTE:
            return GateOutcome("location", EligibilityStatus.ELIGIBLE, f"Remote within {home.country}; the candidate records a location in {home.country} ({candidate_location}).", ConfidenceLevel.MEDIUM)
        if geo is GeoClass.COUNTRY_OTHER:
            return GateOutcome(
                "location",
                EligibilityStatus.UNCERTAIN,
                f"Role in {where}; the candidate is in {candidate_location}. Relocation willingness is not recorded, so this is not treated as a block.",
                ConfidenceLevel.LOW,
            )
        if geo in (GeoClass.FOREIGN, GeoClass.FOREIGN_REMOTE):
            abroad = ", ".join(sorted(places.countries - {home.country})) or where
            recorded = (profile.work_authorization or "").strip()
            authorization = f"records '{recorded}'" if recorded else "does not record work authorization"
            if geo is GeoClass.FOREIGN_REMOTE:
                reason = (
                    f"Remote role restricted to {abroad}; the candidate is located in {candidate_location} and the profile {authorization}. "
                    "Residence and authorization in that country are not assumed."
                )
            else:
                reason = (
                    f"Role located in {abroad}; the candidate is located in {candidate_location} and the profile {authorization}. "
                    "Authorization to work there and relocation willingness are not assumed."
                )
            return GateOutcome("location", EligibilityStatus.UNCERTAIN, reason, ConfidenceLevel.LOW)
        if geo is GeoClass.REMOTE_UNSPECIFIED:
            return GateOutcome(
                "location",
                EligibilityStatus.UNCERTAIN,
                f"Remote role that states no country ({where}); whether the employer hires in {home.country} is not confirmed.",
                ConfidenceLevel.LOW,
            )
        if not places.text:
            # Nothing stated: no location constraint to gate on, and no location claim either
            # (admission sees the posting as UNCONFIRMED, never PRIMARY).
            return None
        return GateOutcome(
            "location",
            EligibilityStatus.UNCERTAIN,
            f"The posting's location ({where}) could not be read; it is not assumed to be in {candidate_location}.",
            ConfidenceLevel.LOW,
        )

    def _unparsed_location_gate(self, job: NormalizedJob, candidate_location: str) -> Optional[GateOutcome]:
        """The original on-site-only rule, kept for a profile location the gazetteer cannot place."""
        from app.jobs.models.enums import RemoteType  # local import avoids a cycle

        if job.remote_type is not RemoteType.ON_SITE or not job.location:
            return None

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
