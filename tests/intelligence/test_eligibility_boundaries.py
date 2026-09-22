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


# --------------------------------------------------------------------- #
# Experience gate and US citizenship (first real dry run, 2026-09-13)
# --------------------------------------------------------------------- #

from datetime import date  # noqa: E402

from app.jobs.models.enums import ExperienceLevel  # noqa: E402

TODAY = date(2026, 9, 13)


@dataclass
class FakeJobHistory:
    is_employment: bool


class BrainWithHistory(FakeCareerBrain):
    def __init__(self, profile=None, experience=()):
        super().__init__(profile)
        self._experience = list(experience)

    def get_experience(self):
        return list(self._experience)


def evaluate_on(job, profile=None, experience=()):
    return EligibilityEngine(BrainWithHistory(profile or FakeProfile(graduation_year=2027), experience), today=lambda: TODAY).evaluate_detailed(job)


def test_senior_role_is_ineligible_for_a_student_with_no_employment():
    """Real boards: 'Senior Software Engineer, Product Velocity' was LIKELY_ELIGIBLE for a
    third-year student, and admission picked it over the company's internships."""
    result = evaluate_on(make_job(title="Senior Software Engineer, Product Velocity", experience_level=ExperienceLevel.SENIOR))
    assert result.status is EligibilityStatus.INELIGIBLE
    assert any("Senior-level role" in r and "graduating 2027" in r for r in result.blocking_reasons)


def test_required_years_in_the_qualifications_are_a_hard_gate_for_a_student():
    job = make_job(title="Software Engineer", qualifications=["4+ years of professional software engineering experience, with strong backend expertise."])
    result = evaluate_on(job)
    assert result.status is EligibilityStatus.INELIGIBLE and "4+ years" in result.blocking_reasons[0]


def test_years_mentioned_only_in_free_text_stay_uncertain_not_blocked():
    job = make_job(title="Software Engineer - Reliability", description="Ideally 2+ years of experience with production systems.")
    result = evaluate_on(job)
    assert result.status is EligibilityStatus.UNCERTAIN and not result.blocking_reasons


def test_one_required_year_or_a_mid_level_role_is_uncertain():
    assert evaluate_on(make_job(qualifications=["1+ years of experience with Python"])).status is EligibilityStatus.UNCERTAIN
    assert evaluate_on(make_job(experience_level=ExperienceLevel.MID)).status is EligibilityStatus.UNCERTAIN


def test_internship_and_new_grad_roles_are_untouched_by_the_experience_gate():
    intern = make_job(title="Software Engineer (CPD) - Winter Intern", experience_level=ExperienceLevel.INTERN, graduation_year_requirement=2027)
    result = evaluate_on(intern)
    assert result.status is EligibilityStatus.ELIGIBLE and not any(g.name == "experience" for g in result.gates)
    new_grad = make_job(title="Software Engineer - New Grad (2027)", experience_level=ExperienceLevel.ENTRY_LEVEL, qualifications=["Recent graduate (2027) with a degree in Computer Science"])
    assert not any(g.name == "experience" for g in evaluate_on(new_grad).gates)


def test_the_gate_never_applies_to_a_candidate_with_employment_or_already_graduated():
    senior = make_job(experience_level=ExperienceLevel.SENIOR, qualifications=["5+ years of experience"])
    employed = evaluate_on(senior, experience=[FakeJobHistory(is_employment=True)])
    assert not any(g.name == "experience" for g in employed.gates), "real experience is measured elsewhere, never assumed away"
    graduated = evaluate_on(senior, profile=FakeProfile(graduation_year=2020))
    assert not any(g.name == "experience" for g in graduated.gates)
    unknown_year = evaluate_on(senior, profile=FakeProfile(graduation_year=None))
    assert not any(g.name == "experience" for g in unknown_year.gates), "an unknown year never makes a candidate a student"


def test_us_citizen_title_is_a_work_authorization_gate_decided_by_the_recorded_status():
    """Real boards: 'Software Engineer - Reliability (US Citizen)' was LIKELY_ELIGIBLE for
    a candidate in India whose authorization is not recorded."""
    job = make_job(title="Software Engineer - Reliability (US Citizen)")
    unrecorded = evaluate_on(job, profile=FakeProfile(graduation_year=2027, work_authorization=None))
    assert unrecorded.status is EligibilityStatus.UNCERTAIN and any("limited to US citizens" in u for u in unrecorded.uncertainties)
    other = evaluate_on(job, profile=FakeProfile(graduation_year=2027, work_authorization="Indian citizen, no sponsorship required"))
    assert other.status is EligibilityStatus.INELIGIBLE, "another country's citizenship is not a US citizenship"
    us = evaluate_on(job, profile=FakeProfile(graduation_year=2027, work_authorization="US Citizen"))
    assert us.status is EligibilityStatus.ELIGIBLE
    campus = evaluate_on(make_job(title="Campus Recruiter", description="Focus citizens of every community."), profile=FakeProfile(graduation_year=2027))
    assert not any(g.name == "work_authorization" for g in campus.gates), "no false positive on ordinary words"


def test_zero_minimum_ranges_and_unrelated_numbers_never_gate():
    assert not any(g.name == "experience" for g in evaluate_on(make_job(qualifications=["0-2 years of experience in software development"])).gates)
    assert not any(g.name == "experience" for g in evaluate_on(make_job(description="Join 3000 engineers across 12 offices.")).gates)


@dataclass
class DatedJob:
    is_employment: bool
    start_date: Optional[str] = None
    end_date: Optional[str] = None


def test_a_short_internship_keeps_a_student_pre_career_but_a_real_career_does_not():
    """Real profile (2026-09-13): a 4-month AI/ML internship is employment, and it switched the
    gate off so senior roles were LIKELY again. Under 12 dated months the gate still applies."""
    senior = make_job(title="Senior Machine Learning Engineer", experience_level=ExperienceLevel.SENIOR)
    intern = evaluate_on(senior, experience=[DatedJob(True, "2025-09", "2026-01")])
    assert intern.status is EligibilityStatus.INELIGIBLE and "less than a year of recorded employment" in intern.blocking_reasons[0]
    career = evaluate_on(senior, experience=[DatedJob(True, "2019-01", "2023-06")])
    assert not any(g.name == "experience" for g in career.gates), "years of dated employment are measured elsewhere"
    undated = evaluate_on(senior, experience=[DatedJob(True), DatedJob(True, "2025-09", "2026-01")])
    assert not any(g.name == "experience" for g in undated.gates), "undated employment is unknown, never assumed short"
