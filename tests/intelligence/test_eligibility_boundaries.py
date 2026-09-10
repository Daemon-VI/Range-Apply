"""Boundary tests for :mod:`app.intelligence.services.eligibility_engine`.

The headline regression covered here: a graduation requirement expressing
only a **maximum** year ("graduating by 2028") used to fall through an
``elif`` chain in the old implementation and was never evaluated at all, so
an ineligible candidate was silently reported ELIGIBLE. Every boundary of
every gate is exercised directly against a tiny fake Career Brain so these
tests stay independent of the real career data file.
"""

from dataclasses import dataclass
from typing import Optional

import pytest

from app.intelligence.models.enums import ConfidenceLevel, EligibilityStatus
from app.intelligence.services.eligibility_engine import EligibilityEngine, EligibilityEvaluation
from app.jobs.models.enums import JobSourceType, RemoteType
from app.jobs.models.job import GraduationRequirement, NormalizedJob


@dataclass
class FakeProfile:
    """Just the fields the eligibility gates actually read."""

    graduation_year: Optional[int] = 2027
    location: Optional[str] = None
    work_authorization: Optional[str] = None


class FakeCareerBrain:
    """Minimal stand-in for CareerBrainService's read surface."""

    def __init__(self, profile: Optional[FakeProfile] = None):
        self._profile = profile or FakeProfile()

    def get_profile(self):
        return self._profile

    def get_skills(self):
        return []

    def get_projects(self):
        return []

    def get_experience(self):
        return []

    def get_preferences(self):
        return None


def make_job(**overrides) -> NormalizedJob:
    defaults = dict(
        canonical_key="job-1",
        source=JobSourceType.GREENHOUSE,
        source_job_id="1",
        company="Acme",
        title="Software Engineer",
        original_title="Software Engineer",
        source_url="https://example.com/job/1",
        description="",
        original_description="",
    )
    defaults.update(overrides)
    return NormalizedJob(**defaults)


def evaluate(job: NormalizedJob, profile: FakeProfile) -> EligibilityEvaluation:
    return EligibilityEngine(FakeCareerBrain(profile)).evaluate_detailed(job)


# --------------------------------------------------------------------- #
# Graduation year gate
# --------------------------------------------------------------------- #


def test_maximum_year_only_eligible_below_cap():
    """Regression: a max-only requirement used to be skipped entirely by an
    elif chain, so a candidate who genuinely satisfied it was never even
    evaluated as ELIGIBLE on purpose - it just fell through unnoticed."""
    job = make_job(
        graduation_requirement=GraduationRequirement(
            maximum_year=2028, original_text="graduating by 2028"
        )
    )
    result = evaluate(job, FakeProfile(graduation_year=2027))
    assert result.status == EligibilityStatus.ELIGIBLE


def test_maximum_year_only_ineligible_above_cap():
    """Regression companion: the same max-only requirement must actually
    block a candidate who graduates after the cap - the old bug reported
    this candidate ELIGIBLE too, because the gate never ran."""
    job = make_job(
        graduation_requirement=GraduationRequirement(
            maximum_year=2028, original_text="graduating by 2028"
        )
    )
    result = evaluate(job, FakeProfile(graduation_year=2029))
    assert result.status == EligibilityStatus.INELIGIBLE
    assert result.blocking_reasons


@pytest.mark.parametrize(
    "candidate_year, expected_status",
    [
        (2025, EligibilityStatus.INELIGIBLE),
        (2026, EligibilityStatus.ELIGIBLE),  # boundary itself must pass
        (2027, EligibilityStatus.ELIGIBLE),
    ],
)
def test_minimum_year_only(candidate_year, expected_status):
    job = make_job(graduation_requirement=GraduationRequirement(minimum_year=2026))
    result = evaluate(job, FakeProfile(graduation_year=candidate_year))
    assert result.status == expected_status


@pytest.mark.parametrize(
    "candidate_year, expected_status",
    [
        (2027, EligibilityStatus.ELIGIBLE),
        (2025, EligibilityStatus.INELIGIBLE),
    ],
)
def test_exact_years_list(candidate_year, expected_status):
    job = make_job(
        graduation_requirement=GraduationRequirement(exact_years=[2026, 2027, 2028])
    )
    result = evaluate(job, FakeProfile(graduation_year=candidate_year))
    assert result.status == expected_status


@pytest.mark.parametrize(
    "candidate_year, expected_status",
    [
        (2025, EligibilityStatus.INELIGIBLE),
        (2026, EligibilityStatus.ELIGIBLE),  # lower boundary
        (2027, EligibilityStatus.ELIGIBLE),  # inside the range
        (2028, EligibilityStatus.ELIGIBLE),  # upper boundary
        (2029, EligibilityStatus.INELIGIBLE),
    ],
)
def test_minimum_and_maximum_range(candidate_year, expected_status):
    job = make_job(
        graduation_requirement=GraduationRequirement(minimum_year=2026, maximum_year=2028)
    )
    result = evaluate(job, FakeProfile(graduation_year=candidate_year))
    assert result.status == expected_status


def test_scalar_graduation_year_field_alone_matches():
    """No GraduationRequirement object - just the denormalized scalar field."""
    job = make_job(graduation_year_requirement=2027)
    result = evaluate(job, FakeProfile(graduation_year=2027))
    assert result.status == EligibilityStatus.ELIGIBLE


def test_scalar_graduation_year_field_alone_mismatches():
    job = make_job(graduation_year_requirement=2027)
    result = evaluate(job, FakeProfile(graduation_year=2026))
    assert result.status == EligibilityStatus.INELIGIBLE


def test_graduation_requirement_with_no_usable_years_is_uncertain_not_eligible():
    """A requirement object was detected but nothing could be parsed out of
    it - that must surface as UNCERTAIN, never be rounded up to ELIGIBLE."""
    job = make_job(
        graduation_requirement=GraduationRequirement(original_text="must graduate soon")
    )
    result = evaluate(job, FakeProfile(graduation_year=2027))
    assert result.status == EligibilityStatus.UNCERTAIN
    assert result.status != EligibilityStatus.ELIGIBLE


def test_missing_candidate_graduation_year_is_uncertain():
    """The JD states a constraint but the profile has nothing to check it
    against - honest unknown, not a pass and not a fail."""
    job = make_job(graduation_requirement=GraduationRequirement(minimum_year=2026))
    result = evaluate(job, FakeProfile(graduation_year=None))
    assert result.status == EligibilityStatus.UNCERTAIN


def test_no_graduation_constraint_is_likely_eligible_not_eligible():
    """No hard gate was expressed anywhere in the JD at all. That is not the
    same as passing one - the engine must say "probably fine, unproven",
    not overstate it as ELIGIBLE."""
    job = make_job()
    result = evaluate(job, FakeProfile(graduation_year=2027))
    assert result.status == EligibilityStatus.LIKELY_ELIGIBLE
    assert result.status != EligibilityStatus.ELIGIBLE
    assert any(
        "no hard eligibility constraints" in reason.lower()
        for reason in result.eligibility_reasons
    )


# --------------------------------------------------------------------- #
# Confidence is derived, never hardcoded
# --------------------------------------------------------------------- #


def test_low_extraction_confidence_yields_low_confidence():
    job = make_job(
        graduation_requirement=GraduationRequirement(
            minimum_year=2026, extraction_confidence=0.5
        )
    )
    result = evaluate(job, FakeProfile(graduation_year=2027))
    assert result.status == EligibilityStatus.ELIGIBLE
    assert result.confidence == ConfidenceLevel.LOW


def test_high_extraction_confidence_yields_high_confidence():
    job = make_job(
        graduation_requirement=GraduationRequirement(
            minimum_year=2026, extraction_confidence=0.95
        )
    )
    result = evaluate(job, FakeProfile(graduation_year=2027))
    assert result.status == EligibilityStatus.ELIGIBLE
    assert result.confidence == ConfidenceLevel.HIGH


def test_confidence_tracks_extraction_confidence_not_a_constant():
    """Same ELIGIBLE outcome, different source confidence - the two must
    not collapse to the same hardcoded confidence level."""
    low_job = make_job(
        graduation_requirement=GraduationRequirement(
            minimum_year=2026, extraction_confidence=0.5
        )
    )
    high_job = make_job(
        graduation_requirement=GraduationRequirement(
            minimum_year=2026, extraction_confidence=0.95
        )
    )
    profile = FakeProfile(graduation_year=2027)
    low_result = evaluate(low_job, profile)
    high_result = evaluate(high_job, profile)

    assert low_result.status == high_result.status == EligibilityStatus.ELIGIBLE
    assert low_result.confidence != high_result.confidence


def test_uncertain_gate_never_reports_high_confidence():
    """No path may report HIGH confidence over an unresolved gate."""
    profile = FakeProfile(graduation_year=2027, location="Chennai", work_authorization="")

    unparseable_grad = make_job(
        graduation_requirement=GraduationRequirement(original_text="soon")
    )
    unresolved_work_auth = make_job(
        description="Unfortunately we are not able to sponsor visas for this role."
    )
    unresolved_location = make_job(remote_type=RemoteType.ON_SITE, location="Pune")

    for job in (unparseable_grad, unresolved_work_auth, unresolved_location):
        result = evaluate(job, profile)
        assert result.status == EligibilityStatus.UNCERTAIN
        assert result.confidence != ConfidenceLevel.HIGH


# --------------------------------------------------------------------- #
# Work authorization gate
# --------------------------------------------------------------------- #


def test_sponsorship_block_with_empty_profile_is_uncertain():
    """A JD that cannot sponsor, but the profile records nothing about work
    authorization - must not be silently assumed either way."""
    job = make_job(description="Unfortunately we are not able to sponsor visas for this role.")
    result = evaluate(job, FakeProfile(graduation_year=2027, work_authorization=""))
    assert result.status == EligibilityStatus.UNCERTAIN
    assert result.status != EligibilityStatus.INELIGIBLE
    assert result.status != EligibilityStatus.ELIGIBLE


def test_sponsorship_block_with_citizen_profile_is_eligible():
    job = make_job(description="Unfortunately we are not able to sponsor visas for this role.")
    result = evaluate(
        job, FakeProfile(graduation_year=2027, work_authorization="US Citizen")
    )
    assert result.status == EligibilityStatus.ELIGIBLE


# --------------------------------------------------------------------- #
# Location / on-site gate
# --------------------------------------------------------------------- #


def test_onsite_job_in_different_city_is_uncertain_not_blocked():
    """Relocation willingness is not recorded, so an on-site mismatch must
    never become a silent hard block."""
    job = make_job(remote_type=RemoteType.ON_SITE, location="Bengaluru")
    result = evaluate(job, FakeProfile(graduation_year=2027, location="Hyderabad"))
    assert result.status == EligibilityStatus.UNCERTAIN
    assert result.status != EligibilityStatus.INELIGIBLE


def test_onsite_job_in_candidates_own_city_is_eligible():
    job = make_job(remote_type=RemoteType.ON_SITE, location="Bengaluru")
    result = evaluate(job, FakeProfile(graduation_year=2027, location="Bengaluru"))
    assert result.status == EligibilityStatus.ELIGIBLE
