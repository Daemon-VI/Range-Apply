"""Audit regressions (2026-09-14): experience wording and US work-authorization spelling.

* "More than 3 years of experience" / "Over 5 years of experience" / "3 years' experience"
  were read as no requirement, so a pre-career student was not gated.
* "authorized to work in the U.S." (sentence-final period) named no country, so an
  "Indian citizen" satisfied a United States authorization requirement.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

import pytest

from app.intelligence.extraction.seniority import experience_statements
from app.intelligence.models.enums import EligibilityStatus
from app.intelligence.services.eligibility_engine import EligibilityEngine
from app.jobs.geography import required_work_countries
from app.jobs.models.enums import EmploymentType, JobSourceType
from app.jobs.models.job import NormalizedJob

HYDERABAD = "Hyderabad, Telangana, India"


@dataclass
class Candidate:
    graduation_year: Optional[int] = 2027
    location: Optional[str] = HYDERABAD
    work_authorization: Optional[str] = None


class Brain:
    def __init__(self, candidate: Candidate):
        self.candidate = candidate

    def get_profile(self):
        return self.candidate

    def get_experience(self):
        return []


def make_job(title: str = "Software Engineer", description: str = "", **overrides) -> NormalizedJob:
    values = dict(
        canonical_key="audit-1",
        source=JobSourceType.GREENHOUSE,
        source_job_id="1",
        company="Acme",
        title=title,
        original_title=title,
        source_url="https://example.com/job/1",
        description=description,
        original_description=description,
        location=HYDERABAD,
        locations=[HYDERABAD],
        employment_type=EmploymentType.FULL_TIME,
    )
    values.update(overrides)
    return NormalizedJob(**values)


def engine(candidate: Candidate) -> EligibilityEngine:
    return EligibilityEngine(Brain(candidate), today=lambda: date(2026, 9, 14))


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("More than 3 years of experience in backend development", 3),
        ("Over 5 years of experience with Java", 5),
        ("Nearly 4 years of hands-on experience", 4),
        ("3 years' experience required", 3),
        ("Minimum of 3 years’ experience", 3),
    ],
)
def test_comparative_and_possessive_experience_wording_is_a_requirement(text, years):
    assert [s.years for s in experience_statements([text])] == [years]


@pytest.mark.parametrize(
    "text",
    ["We have over 20 years of experience serving banks", "Trusted by customers for over 20 years", "Founded 12 years ago", "over the past 5 years of experience"],
)
def test_company_history_is_still_not_a_requirement(text):
    assert experience_statements([text]) == []


def test_student_is_ineligible_for_more_than_three_years_of_experience():
    job = make_job(description="<p>We need someone with more than 3 years of experience building distributed systems.</p>")
    result = engine(Candidate()).evaluate_detailed(job)
    assert result.status is EligibilityStatus.INELIGIBLE
    assert any("3+ years" in reason for reason in result.blocking_reasons)


@pytest.mark.parametrize(
    "sentence",
    [
        "Candidates must be authorized to work in the U.S.",
        "You must be authorized to work in the U.S. without sponsorship.",
        "Applicants must be legally authorized to work in the US.",
    ],
)
def test_us_spellings_name_the_united_states(sentence):
    assert required_work_countries(sentence) == frozenset({"United States"})


def test_uk_spelling_names_the_united_kingdom():
    assert required_work_countries("You must have the right to work in the U.K.") == frozenset({"United Kingdom"})


def test_indian_citizen_does_not_satisfy_a_us_authorization_requirement_spelled_u_s():
    job = make_job(description="Candidates must be authorized to work in the U.S.")
    result = engine(Candidate(graduation_year=2020, work_authorization="Indian citizen")).evaluate_detailed(job)
    gate = next(g for g in result.gates if g.name == "work_authorization")
    assert gate.status is EligibilityStatus.UNCERTAIN, gate.reason
    assert "United States" in gate.reason
