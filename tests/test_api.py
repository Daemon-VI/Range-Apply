"""Tests for FastAPI endpoints."""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


class TestHealthEndpoint:
    def test_health(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestProfileEndpoint:
    def test_get_profile(self, client):
        response = client.get("/profile")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Ribhu Siripurapu"
        assert data["graduation_year"] == 2027


class TestSkillsEndpoint:
    def test_get_skills(self, client):
        response = client.get("/skills")
        assert response.status_code == 200
        assert len(response.json()) > 0


class TestProjectsEndpoint:
    def test_get_projects(self, client):
        response = client.get("/projects")
        assert response.status_code == 200
        assert len(response.json()) >= 5

    def test_get_project_by_id(self, client):
        response = client.get("/projects/ticket-engine")
        assert response.status_code == 200
        assert response.json()["name"] == "Ticket Engine"

    def test_get_project_not_found(self, client):
        response = client.get("/projects/nonexistent")
        assert response.status_code == 404


class TestExperienceEndpoint:
    def test_get_experience(self, client):
        response = client.get("/experience")
        assert response.status_code == 200
        assert len(response.json()) >= 1


class TestAchievementsEndpoint:
    def test_get_achievements(self, client):
        response = client.get("/achievements")
        assert response.status_code == 200
        assert len(response.json()) >= 1


class TestPreferencesEndpoint:
    def test_get_preferences(self, client):
        response = client.get("/preferences")
        assert response.status_code == 200
        assert "Software Engineer" in response.json()["target_roles_tier1"]


class TestCareerBrainEndpoint:
    def test_get_career_brain_summary(self, client):
        response = client.get("/career-brain")
        assert response.status_code == 200
        data = response.json()
        assert data["projects_count"] >= 5
        assert data["verified_facts_count"] >= 2

    def test_get_verified_facts(self, client):
        response = client.get("/career-brain/facts/verified")
        assert response.status_code == 200
        assert len(response.json()) >= 2

    def test_get_application_safe_facts(self, client):
        response = client.get("/career-brain/facts/application-safe")
        assert response.status_code == 200
        for fact in response.json():
            assert fact["allowed_for_application"] is True

    def test_search_projects(self, client):
        response = client.get("/career-brain/projects/search", params={"q": "Redis"})
        assert response.status_code == 200
        assert len(response.json()) >= 1

    def test_find_relevant_projects(self, client):
        response = client.get(
            "/career-brain/projects/relevant", params={"role": "Backend Engineer"}
        )
        assert response.status_code == 200
        assert len(response.json()) >= 1
