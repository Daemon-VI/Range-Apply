"""Tests for CareerBrainService."""

import pytest

from app.services.career_brain import CareerBrainService


@pytest.fixture
def service():
    svc = CareerBrainService()
    svc.load()
    return svc


class TestCareerBrainService:
    def test_profile_retrieval(self, service):
        profile = service.get_profile()
        assert profile.name == "Ribhu Siripurapu"
        assert profile.graduation_year == 2027
        assert profile.cgpa == 8.52

    def test_skill_retrieval(self, service):
        skills = service.get_skills()
        assert len(skills) > 0
        verified = service.get_verified_skills()
        assert all(s.is_verified for s in verified)

    def test_project_retrieval(self, service):
        projects = service.get_projects()
        assert len(projects) >= 5
        ticket = service.get_project("ticket-engine")
        assert ticket.name == "Ticket Engine"
        assert "Go" in ticket.technologies

    def test_project_not_found(self, service):
        with pytest.raises(KeyError):
            service.get_project("nonexistent")

    def test_project_search(self, service):
        results = service.search_projects("Redis")
        assert len(results) >= 1
        assert any(p.id == "ticket-engine" for p in results)

    def test_find_relevant_projects(self, service):
        results = service.find_relevant_projects("Backend Engineer")
        assert len(results) >= 1
        assert results[0].id == "ticket-engine"

    def test_verified_facts(self, service):
        facts = service.get_verified_facts()
        assert len(facts) >= 2
        assert all(f.verification_status.value == "VERIFIED" for f in facts)

    def test_application_safe_facts(self, service):
        safe = service.get_application_safe_facts()
        assert len(safe) >= 2
        for fact in safe:
            assert fact.allowed_for_application

    def test_needs_review_facts(self, service):
        review = service.get_needs_review_facts()
        assert any(f.id == "fact-github" for f in review)

    def test_experience_retrieval(self, service):
        experience = service.get_experience()
        assert len(experience) >= 1
        assert all(not e.is_employment for e in experience)

    def test_achievements_retrieval(self, service):
        achievements = service.get_achievements()
        assert len(achievements) >= 1

    def test_preferences_retrieval(self, service):
        prefs = service.get_preferences()
        assert "Software Engineer" in prefs.target_roles_tier1
        assert prefs.graduation_eligibility == 2027

    def test_career_summary(self, service):
        summary = service.get_career_summary()
        assert summary.profile.name == "Ribhu Siripurapu"
        assert summary.projects_count >= 5
        assert summary.verified_facts_count >= 2
