"""Tests for Job normalization and content hashing (Milestone 7)."""

from app.jobs.models.enums import EmploymentType, ExperienceLevel, JobSourceType
from app.jobs.models.raw_job import RawJob
from app.jobs.normalization.normalizer import (
    JobNormalizer,
    compute_canonical_key,
    compute_content_hash,
    normalize_title,
)


def test_compute_content_hash_stability():
    hash1 = compute_content_hash("Stripe", "Backend Engineer", "<p>We are hiring   a backend engineer with Python.</p>")
    hash2 = compute_content_hash("stripe", "Backend Engineer", "We are hiring a backend engineer with Python.")
    assert hash1 == hash2
    assert len(hash1) == 64  # full sha256


def test_compute_canonical_key_same_location():
    key1 = compute_canonical_key("Uber", "Software Engineer", "San Francisco, CA")
    key2 = compute_canonical_key("uber", "software engineer", "san francisco, ca")
    assert key1 == key2


def test_compute_canonical_key_different_location_remain_separate():
    key_sf = compute_canonical_key("Uber", "Software Engineer", "San Francisco, CA")
    key_hyd = compute_canonical_key("Uber", "Software Engineer", "Hyderabad, India")
    assert key_sf != key_hyd


def test_normalize_title():
    assert normalize_title("Stripe - Software Engineer", "Stripe") == "Software Engineer"
    assert normalize_title("[Remote] Backend Engineer (Full-time)") == "Backend Engineer"
    assert normalize_title("SDE Intern (Summer 2027)") == "SDE Intern (Summer 2027)"


def test_job_normalizer():
    raw = RawJob(
        source=JobSourceType.GREENHOUSE,
        source_job_id="999",
        source_url="https://boards.greenhouse.io/stripe/jobs/999",
        discovered_url="https://boards.greenhouse.io/stripe",
        raw_title="Stripe - Backend Engineering Intern",
        raw_content="<p>Summer 2027 internship for students graduating in 2027. Experience with Go, Python, and PostgreSQL.</p>",
        content_type="html",
        raw_location="Seattle, WA; Remote",
        raw_metadata={"board_token": "stripe", "apply_url": "https://boards.greenhouse.io/stripe/jobs/999#apply"},
    )

    normalizer = JobNormalizer()
    job = normalizer.normalize(raw, company_name="Stripe")

    assert job.company == "Stripe"
    assert job.title == "Backend Engineering Intern"
    assert job.original_title == "Stripe - Backend Engineering Intern"
    assert job.employment_type == EmploymentType.INTERNSHIP
    assert job.experience_level == ExperienceLevel.INTERN
    assert job.graduation_requirement is not None
    assert job.graduation_requirement.minimum_year == 2027
    assert "Go" in job.required_skills
    assert "Python" in job.required_skills
    assert job.location == "Seattle, WA"
    assert len(job.locations) == 2
    assert job.application_url == "https://boards.greenhouse.io/stripe/jobs/999#apply"
    assert len(job.content_hash) == 64
