"""Backend audit (2026-09-14): eligibility false positives / negatives found with realistic posting text.

* Graduation lists and months: "a 2025 or 2026 graduate" kept only 2026 (a 2025
  graduate was INELIGIBLE); "Must graduate before December 2027" and
  "graduate in December 2026" (real Greenhouse / Ashby postings) were not read.
* Study durations read as work experience: "at least 2 years of undergraduate
  study" made an internship INELIGIBLE for a student.
* Negated work authorization read as its opposite: "Not a US citizen" passed a
  US-citizens-only role; "I do not require sponsorship" read as needing it.
* A security clearance was satisfied by any recorded citizenship.
* An unrecognised primary location target switched geographic targeting off.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

import pytest

from app.intelligence.extraction.seniority import experience_statements
from app.intelligence.models.enums import EligibilityStatus
from app.intelligence.services.eligibility_engine import EligibilityEngine
from app.jobs.extraction.deterministic import extract_graduation_requirement
from app.jobs.geography import GeoTier, policy_from_preferences
from app.jobs.models.enums import JobSourceType
from app.jobs.models.job import NormalizedJob
from app.models.preference import Preference


@pytest.mark.parametrize(
    "text, exact, minimum, maximum",
    [
        # Real postings from the 2026-09-14 database.
        ("You are a recent graduate with less than 12 months of full-time working experience or a 2025 or 2026 graduate with", [2025, 2026], 2025, 2026),
        ("Must graduate before December 2027. This internship will take place from January 25", [], None, 2027),
        ("Must graduate before Summer 2028.", [], None, 2028),
        ("To be eligible for this role, you need to graduate in December 2026 and be able to start full time by January/February 2027.", [2026], 2026, 2026),
        ("Recent graduate (2027) with a degree in Computer Science", [2027], 2027, 2027),
        # Common variants.
        ("Students graduating in 2026 and 2027 are welcome.", [2026, 2027], 2026, 2027),
        ("Open to 2026 & 2027 graduates.", [2026, 2027], 2026, 2027),
        ("2025, 2026, 2027 passouts only", [2025, 2026, 2027], 2025, 2027),
        ("You must be graduating by Spring 2027.", [], None, 2027),
        ("graduating between December 2025 and August 2026", [2025, 2026], 2025, 2026),
        # Unchanged behaviour.
        ("We are seeking students graduating between 2026 and 2028 in Computer Science.", [2026, 2027, 2028], 2026, 2028),
        ("Eligible candidates must be from the 2027 batch.", [2027], 2027, 2027),
        ("2027 graduates of Circuital branches only - Available from January 2027 to May 2027", [2027], 2027, 2027),
    ],
)
def test_graduation_lists_months_and_seasons_are_read(text, exact, minimum, maximum):
    requirement = extract_graduation_requirement(text)
    assert requirement is not None, text
    assert (requirement.exact_years, requirement.minimum_year, requirement.maximum_year) == (exact, minimum, maximum)


def test_a_listed_earlier_graduation_year_is_not_ineligible():
    requirement = extract_graduation_requirement("Minimum requirements: a 2025 or 2026 graduate.")
    posting = _job(graduation_requirement=requirement, graduation_year_requirement=requirement.minimum_year)
    assert _evaluate(posting, Candidate(graduation_year=2025)).status is EligibilityStatus.ELIGIBLE
    assert _evaluate(posting, Candidate(graduation_year=2027)).status is EligibilityStatus.INELIGIBLE


@pytest.mark.parametrize(
    "text",
    [
        "Must have completed at least 2 years of undergraduate study",
        "Minimum 2 years of college completed",
        "Graduating within the next 2 years with experience in Python",
        "Currently pursuing a 5 year integrated M.Tech with experience in ML",
    ],
)
def test_study_durations_are_not_work_experience(text):
    assert experience_statements([text]) == []
    intern = _job(title="Software Engineer Intern", qualifications=[text])
    assert not any(g.name == "experience" for g in _evaluate(intern).gates), text


def test_real_experience_requirements_still_block_a_student():
    assert [s.years for s in experience_statements(["Bachelor's degree and 2 years of professional experience"])] == [2]
    posting = _job(qualifications=["At least 3 years of experience building backend services"])
    assert _evaluate(posting).status is EligibilityStatus.INELIGIBLE


@dataclass
class Candidate:
    graduation_year: Optional[int] = 2027
    location: Optional[str] = None
    work_authorization: Optional[str] = None


class Brain:
    def __init__(self, candidate: Candidate):
        self.candidate = candidate

    def get_profile(self):
        return self.candidate

    def get_experience(self):
        return []


def _job(**overrides) -> NormalizedJob:
    values = dict(
        canonical_key="audit-1",
        source=JobSourceType.GREENHOUSE,
        source_job_id="1",
        company="Acme",
        title="Software Engineer",
        original_title="Software Engineer",
        source_url="https://example.com/job/1",
        description="",
        original_description="",
    )
    values.update(overrides)
    if "description" in overrides and "original_description" not in overrides:
        values["original_description"] = overrides["description"]
    return NormalizedJob(**values)


def _evaluate(posting: NormalizedJob, candidate: Optional[Candidate] = None):
    return EligibilityEngine(Brain(candidate or Candidate()), today=lambda: date(2026, 9, 14)).evaluate_detailed(posting)


def _authorization(description: str, recorded: str, title: str = "Software Engineer"):
    result = _evaluate(_job(title=title, original_title=title, description=description), Candidate(work_authorization=recorded))
    gate = next(g for g in result.gates if g.name == "work_authorization")
    return gate.status


def test_a_negated_us_citizenship_never_satisfies_a_us_citizens_only_role():
    assert _authorization("Must be a U.S. citizen.", "Not a US citizen; authorized to work in India") is EligibilityStatus.INELIGIBLE
    assert _authorization("", "Non-US citizen", title="Software Engineer (US Citizen)") is EligibilityStatus.INELIGIBLE
    assert _authorization("", "US Citizen", title="Software Engineer (US Citizen)") is EligibilityStatus.ELIGIBLE


def test_a_negated_status_never_satisfies_a_country_authorization_requirement():
    us = "Applicants must be authorized to work in the United States."
    assert _authorization(us, "Not a US citizen; authorized to work in India") is EligibilityStatus.UNCERTAIN
    india = "Candidates must be authorized to work in India."
    assert _authorization(india, "Non-citizen, no work authorization in India") is not EligibilityStatus.ELIGIBLE
    assert _authorization(india, "Indian citizen") is EligibilityStatus.ELIGIBLE
    assert _authorization(india, "Not a US citizen; authorized to work in India") is EligibilityStatus.ELIGIBLE


def test_sponsorship_statements_are_read_with_their_negation():
    no_sponsorship = "Unfortunately we are unable to sponsor visas for this role."
    assert _authorization(no_sponsorship, "Requires H-1B sponsorship") is EligibilityStatus.INELIGIBLE
    assert _authorization(no_sponsorship, "Need visa sponsorship") is EligibilityStatus.INELIGIBLE
    assert _authorization(no_sponsorship, "US citizen, I do not require sponsorship") is EligibilityStatus.ELIGIBLE
    assert _authorization(no_sponsorship, "Indian citizen, no sponsorship required") is EligibilityStatus.ELIGIBLE


def test_a_security_clearance_is_never_satisfied_by_citizenship_alone():
    for text in ("This role requires an active Secret security clearance.", "Must be able to obtain a security clearance."):
        assert _authorization(text, "Indian citizen") is EligibilityStatus.UNCERTAIN
        assert _authorization(text, "US Citizen") is EligibilityStatus.UNCERTAIN
    result = _evaluate(_job(description="No security clearance required."), Candidate(work_authorization="Indian citizen"))
    assert not any(g.name == "work_authorization" for g in result.gates)


def test_an_unrecognised_primary_target_keeps_geographic_targeting_on():
    hyderabad = "Hyderabad, Telangana, India"
    typo = policy_from_preferences(Preference(location_primary="Hydrabad"), hyderabad)
    assert typo.active and typo.target.country == "India"
    assert not typo.assess_job("San Francisco, CA").in_policy, "a typo must not put every US posting back in policy"
    small_city = policy_from_preferences(Preference(location_primary="Tirupati"), hyderabad)
    assert small_city.assess_job("Tirupati, Andhra Pradesh").tier is GeoTier.PRIMARY
    assert not policy_from_preferences(Preference(location_primary="Tirupati"), None).active, "no country known anywhere: unchanged"
