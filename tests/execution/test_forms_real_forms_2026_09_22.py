"""Field mapping against real application forms gathered 2026-09-22.

Okta, MongoDB, Rubrik, Everpure, Zeta Global, HCLTech, Notion, Acme (mock):
comboboxes with no rendered options, yes/no button pairs, the country /
prior-employment guards, consent checkboxes, education sub-questions,
exact-vs-topic experience years, and the current-employer facts.
"""

from app.execution.forms import choose_option, map_fields
from app.execution.models import (
    ArtifactView,
    ATSFamily,
    BankAnswerView,
    ExecutionPackage,
    ExecutionTarget,
    FieldAnswerSource,
    FieldAnswerStatus,
    FieldType,
    FormField,
    FormOption,
    PreparedAnswerView,
)


def _package(answers=(), bank=(), profile=None, resume=True, cover=False, company="Acme") -> ExecutionPackage:
    return ExecutionPackage(
        tenant_id="t",
        candidate_opportunity_id="co",
        opportunity_id="opp",
        preparation_id="prep",
        application_id="app",
        attempt_number=1,
        preparation_version=1,
        preparation_fingerprint="x",
        target=ExecutionTarget(source="GREENHOUSE", ats_family=ATSFamily.GREENHOUSE, canonical_url="https://x", company=company, title="Engineer"),
        resume=ArtifactView(id="r", artifact_type="RESUME", content="...", evidence_keys=["skill-python"], template_version="prep-v1", validation_status="PASSED") if resume else None,
        cover_letter=ArtifactView(id="c", artifact_type="COVER_LETTER", content="...", evidence_keys=[], template_version="prep-v1", validation_status="PASSED") if cover else None,
        answers=[PreparedAnswerView(id=f"a{i}", question=q, question_key=k, category=c, answer=a, source="ANSWER_BANK", status=s) for i, (q, k, c, a, s) in enumerate(answers)],
        answer_bank=[BankAnswerView(id=f"b{i}", category=c, question_key=k, answer=a) for i, (k, c, a) in enumerate(bank)],
        profile=profile or {"name": "Ribhu S", "email": "r@example.com"},
    )


def _field(label, kind=FieldType.TEXT, required=True, options=()):
    return FormField(label=label, field_type=kind, required=required, options=[FormOption(label=o) for o in options])


# --------------------------------------------------------------------- #
# (a) combobox, no rendered options
# --------------------------------------------------------------------- #


def test_country_combobox_with_no_options_answers_from_profile():
    """Greenhouse / Ashby style searchable dropdown: options only render once
    someone types, so the answer is carried as text for the executor to pick
    on the page.

    BUG (see report): the identity-field loop in ``_map_one`` unconditionally
    ``break``s on the first pattern whose fact is not in ``_OPTION_SAFE_FACTS``
    (app/execution/forms.py, the loop right after ``picks_option = ...``), so
    it never reaches the "country" pattern -- the only pattern in that list
    that a choice/combobox field is allowed to use. A "Country" combobox is
    therefore never answered from the profile at all. This assertion encodes
    the intended behaviour and currently fails.
    """
    pkg = _package(profile={"country": "India", "location": "Hyderabad, Telangana, India"})
    answer = map_fields([_field("Country", FieldType.COMBOBOX)], pkg)[0]
    assert answer.status is FieldAnswerStatus.ANSWERED
    assert answer.answer == "India"
    assert answer.selected_values == []
    assert answer.source is FieldAnswerSource.PROFILE
    assert "chosen on the page" in answer.reason


def test_location_city_combobox_with_no_options_answers_from_profile():
    pkg = _package(profile={"country": "India", "location": "Hyderabad, Telangana, India"})
    answer = map_fields([_field("Location (City)", FieldType.COMBOBOX)], pkg)[0]
    assert answer.status is FieldAnswerStatus.ANSWERED
    assert answer.answer == "Hyderabad, Telangana, India"


def test_sponsorship_combobox_with_no_options_uses_the_bank():
    pkg = _package(bank=[("will you now or in the future require sponsorship", "sponsorship", "No")])
    answer = map_fields([_field("Will you now or in the future require sponsorship?", FieldType.COMBOBOX)], pkg)[0]
    assert answer.status is FieldAnswerStatus.ANSWERED
    assert answer.answer == "No"


def test_yes_no_shaped_combobox_needs_a_yes_or_a_no_not_a_place_name():
    pkg = _package(profile={"country": "India", "location": "Hyderabad, Telangana, India"})
    answer = map_fields([_field("Are you currently based in Bangalore ?", FieldType.COMBOBOX)], pkg)[0]
    assert answer.status is FieldAnswerStatus.NEEDS_USER_INPUT
    assert "yes / no" in answer.reason


def test_required_combobox_with_no_stored_answer_needs_review():
    pkg = _package(profile={"country": "India", "location": "Hyderabad, Telangana, India"})
    answer = map_fields([_field("Have you worked on end-to-end AI agent development?", FieldType.COMBOBOX)], pkg)[0]
    assert answer.status is FieldAnswerStatus.NEEDS_REVIEW


# --------------------------------------------------------------------- #
# (b) combobox, discovered options
# --------------------------------------------------------------------- #


def test_degree_combobox_with_discovered_options_matches_the_level():
    pkg = _package(profile={"degree": "B.Tech in Computer Science"})
    answer = map_fields([_field("Degree", FieldType.COMBOBOX, options=("High School", "Bachelor's Degree", "Master's Degree"))], pkg)[0]
    assert answer.status is FieldAnswerStatus.ANSWERED
    assert answer.selected_values == ["Bachelor's Degree"]


def test_country_combobox_with_discovered_options_matches_the_country():
    """Same root bug as ``test_country_combobox_with_no_options_answers_from_profile``:
    the country identity pattern is unreachable for a choice/combobox field, so
    this never gets as far as ``choose_option`` at all."""
    pkg = _package(profile={"country": "India"})
    answer = map_fields([_field("Country", FieldType.COMBOBOX, options=("India", "Indonesia", "United States"))], pkg)[0]
    assert answer.status is FieldAnswerStatus.ANSWERED
    assert answer.selected_values == ["India"]


# --------------------------------------------------------------------- #
# (c) yes/no button pairs
# --------------------------------------------------------------------- #


def test_yesno_field_answers_from_the_bank_when_it_is_a_clean_yes_or_no():
    pkg = _package(bank=[("will you now or in the future require notion to sponsor an immigration case", "sponsorship", "No")])
    answer = map_fields([_field("Will you now or in the future require Notion to sponsor an immigration case?", FieldType.YESNO, options=("Yes", "No"))], pkg)[0]
    assert answer.status is FieldAnswerStatus.ANSWERED
    assert answer.selected_values == ["No"]


def test_yesno_field_needs_the_user_when_the_bank_answer_is_not_a_yes_or_no():
    pkg = _package(bank=[("will you now or in the future require notion to sponsor an immigration case", "sponsorship", "Depends on the role")])
    answer = map_fields([_field("Will you now or in the future require Notion to sponsor an immigration case?", FieldType.YESNO, options=("Yes", "No"))], pkg)[0]
    assert answer.status is FieldAnswerStatus.NEEDS_USER_INPUT


# --------------------------------------------------------------------- #
# (d) country guard
# --------------------------------------------------------------------- #


def test_work_authorization_guard_blocks_a_question_naming_another_country():
    # A generic, category-matched bank entry (not the exact wording of the
    # field below): this is the path the guard is written to protect.
    bank = [("are you authorized to work in this country", "work_authorization", "yes")]
    pkg = _package(bank=bank, profile={"country": "India"})
    answer = map_fields([_field("Are you legally authorized to work in the United States?", FieldType.SELECT, options=("Yes", "No"))], pkg)[0]
    assert answer.status is FieldAnswerStatus.NEEDS_USER_INPUT
    assert "another country" in answer.reason


def test_work_authorization_guard_allows_a_question_naming_no_country():
    bank = [("are you authorized to work in this country", "work_authorization", "yes")]
    pkg = _package(bank=bank, profile={"country": "India"})
    answer = map_fields([_field("Are you authorized to work in the country you are applying to?", FieldType.SELECT, options=("Yes", "No"))], pkg)[0]
    assert answer.status is FieldAnswerStatus.ANSWERED
    assert answer.selected_values == ["Yes"]


def test_work_authorization_guard_does_not_apply_with_no_country_on_record():
    bank = [("are you authorized to work in this country", "work_authorization", "yes")]
    pkg = _package(bank=bank, profile={})
    answer = map_fields([_field("Are you legally authorized to work in the United States?", FieldType.SELECT, options=("Yes", "No"))], pkg)[0]
    assert answer.status is FieldAnswerStatus.ANSWERED


# --------------------------------------------------------------------- #
# (e) prior employment guard
# --------------------------------------------------------------------- #


def test_prior_employment_guard_blocks_when_the_profile_records_this_employer():
    bank = [("have you ever worked here before", "prior_employment", "No")]
    pkg = _package(bank=bank, profile={"employers": ["HCLTech"]}, company="HCLTech")
    answer = map_fields([_field("Have you ever worked at HCLTech before?", FieldType.YESNO, options=("Yes", "No"))], pkg)[0]
    assert answer.status is FieldAnswerStatus.NEEDS_USER_INPUT


def test_prior_employment_guard_allows_a_different_employer():
    bank = [("have you ever worked here before", "prior_employment", "No")]
    pkg = _package(bank=bank, profile={"employers": ["HCLTech"]}, company="Acme")
    answer = map_fields([_field("Have you ever worked at HCLTech before?", FieldType.YESNO, options=("Yes", "No"))], pkg)[0]
    assert answer.status is FieldAnswerStatus.ANSWERED
    assert answer.selected_values == ["No"]


# --------------------------------------------------------------------- #
# (f) consent
# --------------------------------------------------------------------- #


def test_required_consent_checkbox_is_ticked_from_the_bank():
    bank = [("i acknowledge", "consent", "Yes")]
    pkg = _package(bank=bank)
    answer = map_fields([_field("I acknowledge", FieldType.CHECKBOX, required=True, options=("I acknowledge",))], pkg)[0]
    assert answer.status is FieldAnswerStatus.ANSWERED
    assert answer.selected_values


def test_optional_consent_checkbox_is_skipped():
    bank = [("i acknowledge", "consent", "Yes")]
    pkg = _package(bank=bank)
    label = "Yes, Acme can contact me about future job opportunities. Privacy policy"
    answer = map_fields([_field(label, FieldType.CHECKBOX, required=False, options=(label,))], pkg)[0]
    assert answer.status is FieldAnswerStatus.SKIPPED
    assert "optional" in answer.reason


def test_required_text_that_is_not_consent_is_never_answered():
    bank = [("i acknowledge", "consent", "Yes")]
    pkg = _package(bank=bank)
    answer = map_fields([_field("Please review the NDA and indicate your agreement by typing 'I agree'", FieldType.TEXT, required=True)], pkg)[0]
    assert answer.status is not FieldAnswerStatus.ANSWERED


def test_required_text_that_classifies_as_consent_needs_the_candidate():
    """A typed acknowledgement (not a tick) is the person's own words."""
    bank = [("i acknowledge", "consent", "Yes")]
    pkg = _package(bank=bank)
    answer = map_fields([_field("I acknowledge the terms and conditions", FieldType.TEXT, required=True)], pkg)[0]
    assert answer.status is FieldAnswerStatus.NEEDS_USER_INPUT


# --------------------------------------------------------------------- #
# (g) education facts
# --------------------------------------------------------------------- #


def test_education_sub_questions_answer_from_the_matching_profile_fact():
    pkg = _package(profile={"college": "IIT Hyderabad", "cgpa": "8.3", "branch": "CSE"}, bank=[("graduation year", "education", "2027")])
    university = map_fields([_field("University *", FieldType.TEXT)], pkg)[0]
    cgpa = map_fields([_field("CGPA/GPA *", FieldType.TEXT)], pkg)[0]
    discipline = map_fields([_field("Discipline", FieldType.TEXT)], pkg)[0]
    assert university.status is FieldAnswerStatus.ANSWERED and university.answer == "IIT Hyderabad"
    assert cgpa.status is FieldAnswerStatus.ANSWERED and cgpa.answer == "8.3"
    assert discipline.status is FieldAnswerStatus.ANSWERED and discipline.answer == "CSE"


def test_education_category_is_never_shared_across_different_facts():
    """A bank entry for one education sub-question (graduation year) must
    never answer a different one (university): the category is exact-only."""
    pkg = _package(profile={"college": "IIT Hyderabad"}, bank=[("graduation year", "education", "2027")])
    university = map_fields([_field("University *", FieldType.TEXT)], pkg)[0]
    assert university.answer == "IIT Hyderabad"
    assert university.answer != "2027"


# --------------------------------------------------------------------- #
# (h) experience years
# --------------------------------------------------------------------- #


def test_total_experience_answers_from_the_bank_by_category():
    pkg = _package(bank=[("years of professional experience", "experience_years", "2")])
    answer = map_fields([_field("What is your total experience?", FieldType.TEXTAREA)], pkg)[0]
    assert answer.status is FieldAnswerStatus.ANSWERED
    assert answer.answer == "2"


def test_topic_specific_experience_years_is_exact_only():
    pkg = _package(bank=[("years of professional experience", "experience_years", "2")])
    answer = map_fields([_field("How many years of experience do you have in building conversational AI?", FieldType.TEXTAREA)], pkg)[0]
    assert answer.status is FieldAnswerStatus.NEEDS_USER_INPUT


# --------------------------------------------------------------------- #
# (i) current employer
# --------------------------------------------------------------------- #


def test_current_company_answers_by_exact_question():
    pkg = _package(bank=[("current company", "current_employer", "Student")])
    answer = map_fields([_field("Current company", FieldType.TEXT)], pkg)[0]
    assert answer.status is FieldAnswerStatus.ANSWERED
    assert answer.answer == "Student"


def test_current_employer_answers_by_category_for_different_wording():
    pkg = _package(bank=[("current company", "current_employer", "Student")])
    answer = map_fields([_field("Who is your current (or most recent) employer?", FieldType.TEXT)], pkg)[0]
    assert answer.status is FieldAnswerStatus.ANSWERED
    assert answer.answer == "Student"


def test_current_employer_with_no_bank_entry_needs_the_user():
    pkg = _package()
    answer = map_fields([_field("Current company", FieldType.TEXT)], pkg)[0]
    assert answer.status is FieldAnswerStatus.NEEDS_USER_INPUT
    assert answer.reason.startswith("current_employer")


# --------------------------------------------------------------------- #
# (j) choose_option
# --------------------------------------------------------------------- #


def test_choose_option_exact_match_among_look_alikes():
    assert choose_option(["India", "Indonesia", "British Indian Ocean Territory"], "India") == "India"


def test_choose_option_exact_place_wins_over_a_shared_city_name():
    labels = ["Hyderabad, Telangana, India", "Hyderabad, Sindh, Pakistan"]
    assert choose_option(labels, "Hyderabad, Telangana, India") == labels[0]


def test_choose_option_yes_no_by_polarity():
    assert choose_option(["Yes", "No"], "No, I do not require sponsorship") == "No"


def test_choose_option_ambiguous_polarity_is_none():
    assert choose_option(["Yes", "No"], "Maybe") is None


def test_choose_option_degree_level():
    assert choose_option(["Bachelor's Degree", "Master's Degree"], "B.Tech in CSE", category="education") == "Bachelor's Degree"


def test_choose_option_exact_wins_over_a_longer_containing_option():
    assert choose_option(["Remote", "Remote (India)"], "Remote") == "Remote"


def test_choose_option_empty_text_is_none():
    assert choose_option(["A", "B"], "") is None


# --------------------------------------------------------------------- #
# (k) identity
# --------------------------------------------------------------------- #


def test_preferred_name_answers_from_the_profile_first_name():
    pkg = _package(profile={"first_name": "Ribhu"})
    answer = map_fields([_field("Preferred Name", FieldType.TEXT)], pkg)[0]
    assert answer.status is FieldAnswerStatus.ANSWERED
    assert answer.answer == "Ribhu"
