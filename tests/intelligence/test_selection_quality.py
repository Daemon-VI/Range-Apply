"""Selection quality (2026-09-14): role relevance, fit safeguards and seniority.

Real queue before the fix: Affiliate Marketing, Math Video Creator, Product
Support and a finance "Associate Analyst" were admitted for a CSE / AI & Data
Science student, "Motion Designer" scored 69, a sparse "Cloud Network Engineer
II" scored 100, and "Associate Architect (9 - 12 Years)" was ELIGIBLE.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

import pytest

from app.intelligence.extraction.seniority import experience_statements, title_seniority
from app.intelligence.models.enums import (
    ConfidenceLevel,
    EligibilityStatus,
    EvidenceStrength,
    MatchStatus,
    RequirementCategory,
    Strictness,
)
from app.intelligence.models.requirements import RequirementAssessment, StructuredRequirement
from app.intelligence.services.eligibility_engine import EligibilityEngine
from app.intelligence.services.fit_scoring_engine import (
    LOW_COVERAGE_FIT_CAP,
    UNRELATED_ROLE_FIT_CAP,
    FitScoringEngine,
)
from app.intelligence.services.preference_evaluator import PreferenceEvaluator
from app.intelligence.services.role_relevance import (
    Relevance,
    RoleClass,
    assess_relevance,
    classify_role,
)
from app.jobs.geography import GeoTier, policy_from_preferences
from app.jobs.models.enums import EmploymentType, JobSourceType
from app.jobs.models.job import NormalizedJob
from app.models.preference import Preference

HYDERABAD = "Hyderabad, Telangana, India"
TARGETS = Preference(
    target_roles_tier1=["Machine Learning Engineer", "AI/ML Engineer", "Applied AI Engineer", "Software Engineer", "Backend Engineer"],
    target_roles_tier2=["Android Engineer", "Mobile Engineer", "Full Stack Engineer", "Data Scientist"],
    target_domains=["Production AI/ML"],
)


@dataclass
class Candidate:
    graduation_year: Optional[int] = 2027
    location: Optional[str] = HYDERABAD
    work_authorization: Optional[str] = None


class Brain:
    def get_profile(self):
        return Candidate()

    def get_skills(self):
        return []

    def get_projects(self):
        return []

    def get_experience(self):
        return []

    def get_preferences(self):
        return TARGETS


def job(title: str, description: str = "", **overrides) -> NormalizedJob:
    defaults = dict(
        canonical_key="sq-1",
        source=JobSourceType.GREENHOUSE,
        source_job_id="1",
        company="Acme",
        title=title,
        original_title=title,
        source_url="https://example.com/job/1",
        description=description,
        original_description=description,
        location="Hyderabad, Telangana, India",
        locations=["Hyderabad, Telangana, India"],
        employment_type=EmploymentType.FULL_TIME,
    )
    defaults.update(overrides)
    return NormalizedJob(**defaults)


def eligibility(posting: NormalizedJob):
    return EligibilityEngine(Brain(), today=lambda: date(2026, 9, 14)).evaluate_detailed(posting)


def skill(name: str, status: MatchStatus = MatchStatus.MATCHED) -> RequirementAssessment:
    requirement = StructuredRequirement(id=name, original_text=name, normalized_name=name, category=RequirementCategory.TECHNICAL_SKILL, strictness=Strictness.REQUIRED)
    strength = EvidenceStrength.DIRECT_VERIFIED if status is MatchStatus.MATCHED else EvidenceStrength.NONE
    return RequirementAssessment(requirement=requirement, status=status, evidence_strength=strength, confidence=ConfidenceLevel.HIGH, impact="high", explanation="test")


def score(posting: NormalizedJob, assessments: list):
    preferences = PreferenceEvaluator().evaluate(posting, TARGETS, profile_location=HYDERABAD)
    context = {"description_length": len(posting.description), "technical_requirement_count": len(assessments)}
    return FitScoringEngine().score(assessments, eligibility(posting), preferences, job_context=context), preferences


MARKETING = "Drive affiliate partnerships, social PR campaigns and brand marketing across publishers; report campaign ROI in SQL dashboards and Python notebooks. " * 2


# ------------------------------------------------------------------ #
# role families
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "title, family, role_class",
    [
        ("Software Engineer, Infrastructure", "software", RoleClass.TECHNICAL),
        ("Software Development Engineer III -Backend", "software", RoleClass.TECHNICAL),
        ("Machine Learning Engineer", "ml_ai", RoleClass.TECHNICAL),
        ("Data Scientist, Growth", "data_science", RoleClass.TECHNICAL),
        ("Data Engineer", "data_engineering", RoleClass.TECHNICAL),
        ("Cloud Network Engineer II", "devops_cloud", RoleClass.TECHNICAL),
        ("Field Security Engineer", "security", RoleClass.TECHNICAL),
        ("Site Reliability Engineer", "devops_cloud", RoleClass.TECHNICAL),
        ("Android Developer", "mobile", RoleClass.TECHNICAL),
        ("Quality Analyst", "qa_test", RoleClass.TECHNICAL),
        ("Sales Engineer (SLED, K-12)", "solutions_engineering", RoleClass.POTENTIALLY_TECHNICAL),
        ("Technical Support Engineer", "technical_support", RoleClass.POTENTIALLY_TECHNICAL),
        ("Agent Product Builder", "product_management", RoleClass.POTENTIALLY_TECHNICAL),
        ("Business Systems Analyst – Sales Planning & Incentive Systems", "technical_analysis", RoleClass.POTENTIALLY_TECHNICAL),
        ("SAP Consultant", "implementation_consulting", RoleClass.POTENTIALLY_TECHNICAL),
        ("Affiliate Marketing (Social PR)", "marketing", RoleClass.NON_TECHNICAL),
        ("Proprietary Content Creator - ABM II", "content_creative", RoleClass.NON_TECHNICAL),
        ("Motion Designer", "content_creative", RoleClass.NON_TECHNICAL),
        ("Account Executive, AI Sales", "sales", RoleClass.NON_TECHNICAL),
        ("Product Support Specialist - L2 Support", "customer_support", RoleClass.NON_TECHNICAL),
        ("Recruiter", "hr_recruiting", RoleClass.NON_TECHNICAL),
        ("Tax Analyst", "finance_accounting", RoleClass.NON_TECHNICAL),
        ("Quote Operations Associate", "operations", RoleClass.NON_TECHNICAL),
        ("Math Video Creator Freelance (Grades 11-12), India", "content_creative", RoleClass.NON_TECHNICAL),
        ("Join Druva's Talent Community", "talent_pool", RoleClass.NON_TECHNICAL),
    ],
)
def test_role_families_are_read_from_the_title_head(title, family, role_class):
    role = classify_role(title)
    assert (role.family, role.role_class) == (family, role_class)


def test_unrecognised_titles_fall_back_to_the_description_never_to_a_guess():
    finance = "Prepare journal entries, reconcile the general ledger, support audit and tax filings, and manage revenue accounting close. " * 2
    assert classify_role("Associate Analyst", finance, technologies=0).family == "finance_accounting"
    assert classify_role("Associate Analyst", "Work with Python, SQL, Airflow and Spark pipelines.", technologies=4).role_class is RoleClass.POTENTIALLY_TECHNICAL
    assert classify_role("Associate", "Join our team.", technologies=0).role_class is RoleClass.UNKNOWN


def test_relevance_is_relative_to_the_candidates_own_targets():
    assert assess_relevance("Affiliate Marketing", MARKETING, 2, TARGETS).relevance is Relevance.UNRELATED
    assert assess_relevance("Site Reliability Engineer", "", 0, TARGETS).relevance is Relevance.RELEVANT
    assert assess_relevance("Agent Product Builder", "", 4, TARGETS).relevance is Relevance.ADJACENT
    assert assess_relevance("Agent Product Builder", "", 1, TARGETS).relevance is Relevance.WEAK
    marketer = Preference(target_roles_tier1=["Marketing Manager"])
    assert assess_relevance("Affiliate Marketing", MARKETING, 2, marketer).relevance is Relevance.RELEVANT
    assert assess_relevance("Software Engineer", "", 5, marketer).relevance is Relevance.WEAK
    assert assess_relevance("Affiliate Marketing", MARKETING, 2, Preference()).relevance is Relevance.NOT_ASSESSED
    opted_in = TARGETS.model_copy(update={"role_include_unrelated": True})
    assert assess_relevance("Affiliate Marketing", MARKETING, 2, opted_in).in_policy


# ------------------------------------------------------------------ #
# fit safeguards
# ------------------------------------------------------------------ #


def test_unrelated_marketing_role_does_not_receive_high_technical_fit():
    result, preferences = score(job("Affiliate Marketing (Social PR)", MARKETING, technologies=["SQL", "Python"]), [skill("SQL"), skill("Python")])
    assert preferences.role.relevance is Relevance.UNRELATED
    assert result.fit_score <= UNRELATED_ROLE_FIT_CAP
    assert any("capped at 30" in note for note in result.uncertainties)


def test_unrelated_content_role_does_not_receive_high_technical_fit():
    text = "Create proprietary content for account-based marketing: long-form articles, production-ready case studies and webinars. " * 2
    result, _ = score(job("Proprietary Content Creator - ABM II", text), [skill("Python")])
    assert result.fit_score <= UNRELATED_ROLE_FIT_CAP


def test_motion_designer_does_not_get_a_misleading_score():
    text = "We are looking for a Motion Designer to craft production-quality animation for launches and social channels. " * 2
    result, _ = score(job("Motion Designer", text), [])
    assert result.fit_score <= UNRELATED_ROLE_FIT_CAP, "used to be 69 on employment type, 'production' and eligibility"


def test_sparse_description_cannot_reach_an_unjustified_maximum():
    text = "Join our networking team in Hyderabad to keep our cloud connected and our customers happy. " * 2
    unread, _ = score(job("Cloud Network Engineer II", text), [])
    assert unread.fit_score == LOW_COVERAGE_FIT_CAP and unread.fit_score < 70, "used to be 100"
    two_keywords, _ = score(job("Software Engineer", "Python and SQL for our backend services, shipped to production every day."), [skill("Python"), skill("SQL")])
    assert two_keywords.component_scores["technical"] < 1.0
    assert any("Few technical requirements" in note for note in two_keywords.uncertainties)


def test_a_genuinely_technical_data_scientist_role_stays_strong():
    text = "Build experimentation and forecasting models in Python, SQL, pandas, scikit-learn and PyTorch for growth analytics. " * 2
    result, preferences = score(job("Data Scientist, Growth", text), [skill(s) for s in ("Python", "SQL", "Pandas", "Scikit-learn", "PyTorch")])
    assert preferences.role.relevance is Relevance.RELEVANT
    assert result.fit_score >= 70 and not any("capped" in note for note in result.uncertainties)


# ------------------------------------------------------------------ #
# seniority / stated experience
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "text, years",
    [
        ("Associate Architect (9 - 12 Years)", 9),
        ("4+ years of experience in backend development", 4),
        ("2+ years of hands-on experience", 2),
        ("3 years of experience with Java", 3),
        ("Experience: 4 - 6 years", 4),
        ("minimum 5 years in a similar role", 5),
        ("five years of experience building APIs", 5),
        ("5+ years of relevant experience", 5),
        ("Bring 8+ years in VMware administration", 8),
        ("What we're looking for 5+ years in ML systems, with 2+ years authoring CUDA kernels", 5),
    ],
)
def test_common_experience_statements_are_detected(text, years):
    statements = experience_statements([text])
    assert statements and max(s.years for s in statements) == years


@pytest.mark.parametrize(
    "text",
    ["Graduating in 2027 or 2028", "A 3-month project on search ranking", "Trusted by customers for over 20 years", "Founded 12 years ago in Hyderabad", "Join 3000 engineers across 12 offices", "0-2 years of experience", "Build a team in 5 cities"],
)
def test_unrelated_numbers_are_never_experience(text):
    assert experience_statements([text]) == []


def test_preferred_experience_is_marked_preferred():
    assert all(s.preferred for s in experience_statements(["2+ years of experience preferred"]))


def test_student_without_employment_is_ineligible_or_uncertain_for_senior_roles():
    assert eligibility(job("Software Development Engineer III -Backend")).status is EligibilityStatus.INELIGIBLE
    assert eligibility(job("Java Architect")).status is EligibilityStatus.INELIGIBLE
    assert eligibility(job("Staff Machine Learning Engineer")).status is EligibilityStatus.INELIGIBLE
    notion = "You have 4+ years of experience building distributed infrastructure at scale."
    assert eligibility(job("Software Engineer, Infrastructure", notion)).status is EligibilityStatus.INELIGIBLE
    assert eligibility(job("Cloud Network Engineer II")).status is EligibilityStatus.UNCERTAIN
    assert eligibility(job("Software Engineer", "Ideally 2+ years of experience with production systems.")).status is EligibilityStatus.UNCERTAIN
    assert eligibility(job("Software Engineer - New Grad (2027)")).status is EligibilityStatus.ELIGIBLE
    assert title_seniority("Associate Product Manager") == "MID"


# ------------------------------------------------------------------ #
# geography defaults still hold
# ------------------------------------------------------------------ #


def test_geography_defaults_still_hold():
    configured = policy_from_preferences(Preference(location_include_unconfirmed=False), HYDERABAD)
    assert configured.assess_job("Hyderabad, Telangana, India").tier is GeoTier.PRIMARY
    remote_only = configured.assess_job("Remote")
    assert remote_only.tier is GeoTier.UNCONFIRMED and not remote_only.in_policy
    default = policy_from_preferences(Preference(), HYDERABAD)
    assert not default.allow_international and not default.assess_job("San Francisco, CA").in_policy
    assert not default.include_other_cities and not default.assess_job("Bengaluru").in_policy


def test_a_description_word_alone_never_makes_a_plain_title_senior():
    # Real run: Sarvam "Platform Engineer - AI Infrastructure" was stored SENIOR from the description's
    # bare "5+ years" regex and blocked, while the title and the stated requirements say nothing senior.
    from app.jobs.models.enums import ExperienceLevel

    stored_senior = job("Platform Engineer - AI Infrastructure", "Build GPU clusters with Kubernetes and Slurm.", experience_level=ExperienceLevel.SENIOR)
    assert eligibility(stored_senior).status is not EligibilityStatus.INELIGIBLE
    titled_senior = job("Senior Platform Engineer", "Build GPU clusters.", experience_level=ExperienceLevel.SENIOR)
    assert eligibility(titled_senior).status is EligibilityStatus.INELIGIBLE
    stated = job("Platform Engineer", "You bring 6+ years of experience operating GPU clusters.", experience_level=ExperienceLevel.SENIOR)
    assert eligibility(stated).status is EligibilityStatus.INELIGIBLE
