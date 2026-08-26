"""Tests for deterministic extraction (Milestone 6)."""

import pytest

from app.jobs.extraction.deterministic import (
    extract_employment_type,
    extract_experience_level,
    extract_graduation_requirement,
    extract_remote_type,
    extract_salary,
    extract_skills_and_technologies,
)
from app.jobs.models.enums import EmploymentType, ExperienceLevel, RemoteType


def test_extract_employment_type():
    assert extract_employment_type("Software Engineering Intern", "", {}) == EmploymentType.INTERNSHIP
    assert extract_employment_type("Senior Backend Engineer", "This is a full-time role", {}) == EmploymentType.FULL_TIME
    assert extract_employment_type("Contract Developer", "", {}) == EmploymentType.CONTRACT
    assert extract_employment_type("Engineer", "", {"employmentType": "Intern"}) == EmploymentType.INTERNSHIP


def test_extract_remote_type():
    assert extract_remote_type("Backend Engineer", "Remote, US", "", {}) == RemoteType.REMOTE
    assert extract_remote_type("Software Engineer", "San Francisco, CA", "We offer a hybrid work environment", {}) == RemoteType.HYBRID
    assert extract_remote_type("DevOps Engineer", "New York, NY", "", {"workplaceType": "OnSite"}) == RemoteType.ON_SITE


def test_extract_experience_level():
    assert extract_experience_level("Software Engineering Intern", "") == ExperienceLevel.INTERN
    assert extract_experience_level("Staff Distributed Systems Engineer", "") == ExperienceLevel.SENIOR
    assert extract_experience_level("Junior Developer", "") == ExperienceLevel.ENTRY_LEVEL
    assert extract_experience_level("Software Engineer", "Looking for 0-2 years of experience") == ExperienceLevel.ENTRY_LEVEL
    assert extract_experience_level("Backend Engineer", "Requires 5+ years of Python") == ExperienceLevel.SENIOR


def test_extract_graduation_requirement_between():
    text = "We are seeking students graduating between 2026 and 2028 in Computer Science."
    req = extract_graduation_requirement(text)
    assert req is not None
    assert req.minimum_year == 2026
    assert req.maximum_year == 2028
    assert req.exact_years == [2026, 2027, 2028]
    assert req.extraction_confidence >= 0.9


def test_extract_graduation_requirement_exact():
    text = "Eligible candidates must be from the 2027 batch."
    req = extract_graduation_requirement(text)
    assert req is not None
    assert req.minimum_year == 2027
    assert req.maximum_year == 2027
    assert req.exact_years == [2027]


def test_extract_graduation_requirement_before():
    text = "Candidates with expected graduation by 2028 are encouraged to apply."
    req = extract_graduation_requirement(text)
    assert req is not None
    assert req.maximum_year == 2028


def test_extract_salary():
    assert extract_salary("", {"compensationTierSummary": "$140k - $180k"}) == "$140k - $180k"
    text = "The base salary range for this role is $120,000 - $160,000 annually."
    assert "$120,000 - $160,000 annually" in extract_salary(text, {})
    inr_text = "Compensation: ₹25-35 LPA depending on experience."
    assert "₹25-35 LPA" in extract_salary(inr_text, {})


def test_extract_skills_and_technologies():
    content = "You will build distributed backend services with Go, Python, FastAPI, and PostgreSQL on AWS using Docker and Kubernetes."
    skills, tech = extract_skills_and_technologies(content)
    assert "Python" in skills
    assert "Go" in skills
    assert "FastAPI" in skills
    assert "PostgreSQL" in skills
    assert "AWS" in skills
    assert "Docker" in skills
    assert "Kubernetes" in skills
