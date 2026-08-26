"""Tests for career data models."""

import pytest
from pydantic import ValidationError

from app.models.achievement import Achievement
from app.models.career_fact import CareerFact
from app.models.claim import Claim
from app.models.enums import FactCategory, SkillCategory, VerificationStatus
from app.models.experience import Experience
from app.models.preference import Preference
from app.models.profile import Profile
from app.models.project import Project, ProjectMetric
from app.models.skill import Skill


class TestProfile:
    def test_valid_profile(self):
        profile = Profile(
            name="Ribhu Siripurapu",
            degree="B.Tech CSE (Data Science)",
            branch="CSE (Data Science)",
            college="MGIT, Hyderabad",
            graduation_year=2027,
            current_academic_status="3rd year",
            cgpa=8.52,
            backlogs="No active backlogs",
        )
        assert profile.name == "Ribhu Siripurapu"
        assert profile.graduation_year == 2027


class TestSkill:
    def test_verified_skill(self):
        skill = Skill(
            id="s1",
            name="Python",
            category=SkillCategory.PROGRAMMING,
            verification_status=VerificationStatus.VERIFIED,
        )
        assert skill.is_verified
        assert skill.allowed_for_resume
        assert skill.allowed_for_application

    def test_unverified_skill(self):
        skill = Skill(
            id="s2",
            name="Java",
            category=SkillCategory.PROGRAMMING,
            verification_status=VerificationStatus.UNVERIFIED,
        )
        assert not skill.is_verified
        assert not skill.allowed_for_application


class TestProject:
    def test_project_search(self):
        project = Project(
            id="p1",
            name="Ticket Engine",
            status="Active",
            summary="High-concurrency ticket system",
            technologies=["Go", "Redis"],
            primary_domain="Backend",
            relevant_roles=["Backend Engineer"],
            technical_concepts=["concurrency"],
        )
        assert project.matches_query("Go")
        assert project.matches_query("Backend")
        assert not project.matches_query("Kubernetes")

    def test_relevance_score(self):
        project = Project(
            id="p1",
            name="Ticket Engine",
            status="Active",
            summary="Backend system",
            technologies=["Go", "Redis"],
            primary_domain="Backend / Distributed Systems",
            relevant_roles=["Backend Engineer", "SDE"],
            technical_concepts=["concurrency"],
        )
        score = project.relevance_score("Backend Engineer")
        assert score >= 3.0

    def test_verified_metrics_filter(self):
        project = Project(
            id="p1",
            name="Test",
            status="Active",
            summary="Test",
            metrics=[
                ProjectMetric(name="latency", value="120ms", verification_status=VerificationStatus.VERIFIED),
                ProjectMetric(name="throughput", value="3k", verification_status=VerificationStatus.UNVERIFIED),
            ],
        )
        assert len(project.verified_metrics) == 1


class TestExperience:
    def test_project_experience_not_employment(self):
        exp = Experience(
            id="e1",
            organization="Personal Project",
            role="Developer",
            is_employment=False,
        )
        assert not exp.is_employment


class TestAchievement:
    def test_verified_achievement(self):
        ach = Achievement(
            id="a1",
            title="CGPA 8.52",
            category="academic",
            verification_status=VerificationStatus.VERIFIED,
        )
        assert ach.is_verified


class TestPreference:
    def test_all_target_roles(self):
        pref = Preference(
            target_roles_tier1=["SDE"],
            target_roles_tier2=["Full Stack"],
            target_roles_lower_priority=["IT Support"],
        )
        assert len(pref.all_target_roles) == 3


class TestCareerFact:
    def test_verified_fact_auto_safe(self):
        fact = CareerFact(
            id="f1",
            category=FactCategory.EDUCATION,
            statement="CGPA 8.52",
            verification_status=VerificationStatus.VERIFIED,
        )
        assert fact.allowed_for_resume
        assert fact.allowed_for_application

    def test_inferred_fact_not_safe(self):
        fact = CareerFact(
            id="f2",
            category=FactCategory.IDENTITY,
            statement="Inferred location",
            verification_status=VerificationStatus.INFERRED,
        )
        assert not fact.allowed_for_application


class TestClaim:
    def test_claim_from_fact(self):
        fact = CareerFact(
            id="f1",
            category=FactCategory.EDUCATION,
            statement="Graduating 2027",
            verification_status=VerificationStatus.VERIFIED,
        )
        claim = Claim.from_fact(fact)
        assert claim.statement == "Graduating 2027"
        assert claim.verification_status == VerificationStatus.VERIFIED
