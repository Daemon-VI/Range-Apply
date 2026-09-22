"""Classification of real application-form questions gathered 2026-09-22.

Covers the new categories added so real forms (Okta, MongoDB, Rubrik,
Everpure, Zeta Global, HCLTech, ...) can be completed: CURRENT_EMPLOYER,
CURRENT_TITLE, REFERRAL_SOURCE, PRIOR_EMPLOYMENT, CONFLICT_OF_INTEREST,
CONSENT -- and confirms the categories that must never be answered by a
single approved bank entry shared across every question in the category.
"""

import pytest

from app.preparation.questions import (
    _CATEGORY_SHARED_ANSWERS,
    FACT_CATEGORIES,
    QuestionCategory,
    classify_question,
)


@pytest.mark.parametrize(
    "label, category",
    [
        ("Where do you currently reside?", QuestionCategory.LOCATION),
        ("Location (City)", QuestionCategory.LOCATION),
        ("Location *", QuestionCategory.LOCATION),
        ("Are you currently based in Bangalore ?", QuestionCategory.LOCATION),
        ("Which location are you applying for?", QuestionCategory.OTHER),
        ("Do you have any familial or close personal relationships with any current employees?", QuestionCategory.CONFLICT_OF_INTEREST),
        ("To the best of your knowledge, do you have any family member employed by Okta?", QuestionCategory.CONFLICT_OF_INTEREST),
        ("Do you have any outside business activity(ies) (advisory, consulting)?", QuestionCategory.CONFLICT_OF_INTEREST),
        ("How did you hear about this opportunity?", QuestionCategory.REFERRAL_SOURCE),
        ("Where did you hear about this job?", QuestionCategory.REFERRAL_SOURCE),
        ("Have you ever worked at MongoDB before?", QuestionCategory.PRIOR_EMPLOYMENT),
        ("Have you been employed by Okta, Inc. or any of its subsidiaries?", QuestionCategory.PRIOR_EMPLOYMENT),
        ("Have you previously worked for Everpure (formerly Pure Storage)?", QuestionCategory.PRIOR_EMPLOYMENT),
        ("Are you currently or have you ever been employed by Rubrik?", QuestionCategory.PRIOR_EMPLOYMENT),
        ("Current company", QuestionCategory.CURRENT_EMPLOYER),
        ("Who is your current (or most recent) employer?", QuestionCategory.CURRENT_EMPLOYER),
        ("What is the name of your current employer?", QuestionCategory.CURRENT_EMPLOYER),
        ("What is your current (or most recent) title?", QuestionCategory.CURRENT_TITLE),
        ("Why are you leaving your current role?", QuestionCategory.OTHER),
        ("I acknowledge", QuestionCategory.CONSENT),
        ("Acknowledge/Confirm", QuestionCategory.CONSENT),
        ("I acknowledge that I have read Rubrik's Candidate Privacy Notice", QuestionCategory.CONSENT),
        (
            "By checking this box, I consent to MongoDB collecting, storing, and processing my responses to the demographic data surveys above.",
            QuestionCategory.CONSENT,
        ),
        ("School", QuestionCategory.EDUCATION),
        ("Degree", QuestionCategory.EDUCATION),
        ("Discipline", QuestionCategory.EDUCATION),
        ("University *", QuestionCategory.EDUCATION),
        ("CGPA/GPA *", QuestionCategory.EDUCATION),
        ("Gender Identity (Select one)", QuestionCategory.VOLUNTARY),
        ("Will you now or in the future require sponsorship for Employment?", QuestionCategory.SPONSORSHIP),
        ("What is your total experience?", QuestionCategory.EXPERIENCE_YEARS),
    ],
)
def test_real_form_labels_classify_correctly(label, category):
    assert classify_question(label).category is category


@pytest.mark.parametrize(
    "category",
    [
        QuestionCategory.CURRENT_EMPLOYER,
        QuestionCategory.CURRENT_TITLE,
        QuestionCategory.REFERRAL_SOURCE,
        QuestionCategory.PRIOR_EMPLOYMENT,
        QuestionCategory.CONFLICT_OF_INTEREST,
        QuestionCategory.CONSENT,
    ],
)
def test_new_categories_are_fact_categories(category):
    """Each new category is a candidate fact: never templated, never guessed."""
    assert category in FACT_CATEGORIES


def test_conflict_of_interest_and_education_are_never_shared_by_category():
    """Each question under these headings asks about a different employer or
    a different fact: one approved answer must not answer every question of
    the category."""
    assert QuestionCategory.CONFLICT_OF_INTEREST not in _CATEGORY_SHARED_ANSWERS
    assert QuestionCategory.EDUCATION not in _CATEGORY_SHARED_ANSWERS


@pytest.mark.parametrize(
    "category",
    [
        QuestionCategory.CONSENT,
        QuestionCategory.CURRENT_EMPLOYER,
        QuestionCategory.REFERRAL_SOURCE,
        QuestionCategory.PRIOR_EMPLOYMENT,
    ],
)
def test_generic_fact_categories_are_shared_by_category(category):
    """These candidate facts hold whatever the exact wording: one approved
    entry may answer every question of the category."""
    assert category in _CATEGORY_SHARED_ANSWERS
