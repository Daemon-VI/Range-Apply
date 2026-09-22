"""Titles the first autopilot re-score admitted although they are not technical (2026-09-14)."""

import pytest

from app.intelligence.services.role_relevance import (
    Relevance,
    RoleClass,
    assess_relevance,
    classify_role,
)
from app.models.preference import Preference

PREFS = Preference(target_roles_tier1=["Machine Learning Engineer", "Software Engineer", "Backend Engineer"], target_roles_tier2=["Android Engineer", "Full Stack Engineer", "Data Scientist"])


@pytest.mark.parametrize("title", ["capital partnerships", "Account Development Representative", "M&E Analyst", "Strategic Partnerships Manager", "Monitoring and Evaluation Officer", "ADR - Enterprise"])
def test_non_technical_titles_are_recognised(title):
    role = classify_role(title, description="We use Python, SQL and Salesforce daily.", technologies=3)
    assert role.role_class is RoleClass.NON_TECHNICAL, role
    assessment = assess_relevance(title, "We use Python, SQL and Salesforce daily.", 3, PREFS)
    assert assessment.relevance in (Relevance.UNRELATED, Relevance.WEAK), assessment


@pytest.mark.parametrize("title, expected", [
    ("Partner Engineer", RoleClass.POTENTIALLY_TECHNICAL),
    ("Alliances Architect", RoleClass.POTENTIALLY_TECHNICAL),
    ("Data Analyst (Finance)", RoleClass.TECHNICAL),
    ("DevOps Engineer", RoleClass.TECHNICAL),
    ("SDE II - Salesforce AI", RoleClass.TECHNICAL),
    ("Backend Engineer, Partnerships Platform", RoleClass.TECHNICAL),
])
def test_technical_titles_are_unchanged(title, expected):
    assert classify_role(title).role_class is expected
