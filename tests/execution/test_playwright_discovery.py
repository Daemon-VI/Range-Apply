"""Form discovery and mapping against local fixtures (no network)."""

import pytest

from app.execution.forms import blocking, form_fingerprint, map_fields
from app.execution.models import ATSFamily, ExecutionTarget, FieldAnswerStatus, FieldType
from app.execution.playwright.discovery import scan_page
from app.execution.playwright.strategies import ASHBY, GENERIC, GREENHOUSE, LEVER, strategy_for
from tests.execution.playwright_conftest import fixture_url, make_session, requires_browser
from tests.execution.test_forms import _package

pytestmark = requires_browser


@pytest.fixture(scope="module")
def browser():
    session = make_session()
    yield session
    session.close()


def _scan(browser, name, **query):
    with browser.page() as page:
        page.goto(fixture_url(name, **query))
        return scan_page(page)


def test_greenhouse_fields_types_required_options(browser):
    scan = _scan(browser, "greenhouse.html")
    by_id = {f.external_id: f for f in scan.fields}
    assert by_id["job_application[first_name]"].field_type is FieldType.TEXT and by_id["job_application[first_name]"].required
    assert by_id["job_application[first_name]"].label.startswith("First Name")
    assert by_id["job_application[email]"].field_type is FieldType.EMAIL
    assert by_id["job_application[phone]"].field_type is FieldType.PHONE and not by_id["job_application[phone]"].required
    assert by_id["job_application[resume]"].field_type is FieldType.FILE and by_id["job_application[resume]"].required and ".pdf" in by_id["job_application[resume]"].accept
    assert by_id["job_application[answers_attributes][1][text_value]"].field_type is FieldType.TEXTAREA
    auth = by_id["job_application[answers_attributes][2][boolean_value]"]
    assert auth.field_type is FieldType.SELECT and [o.label for o in auth.options] == ["Yes", "No"] and [o.value for o in auth.options] == ["1", "0"]
    sponsor = by_id["job_application[answers_attributes][3][boolean_value]"]
    assert sponsor.field_type is FieldType.RADIO and sponsor.required and "sponsorship" in sponsor.label.lower()
    assert [o.value for o in sponsor.options] == ["1", "0"]
    privacy = by_id["job_application[answers_attributes][4][boolean_value]"]
    assert privacy.field_type is FieldType.CHECKBOX and not privacy.required
    assert all(f.selector for f in scan.fields)
    assert not scan.captcha and not scan.login_wall and not scan.mfa
    assert any("submit" in b["text"].lower() for b in scan.submit_buttons)
    assert form_fingerprint(scan.fields) == form_fingerprint(_scan(browser, "greenhouse.html").fields)


def test_lever_and_generic_discovery(browser):
    lever = {f.external_id: f for f in _scan(browser, "lever.html").fields}
    assert lever["cards[0][field0]"].field_type is FieldType.DATE and lever["cards[0][field0]"].required
    assert lever["cards[0][field1]"].field_type is FieldType.NUMERIC
    interests = lever["cards[0][field2]"]
    assert interests.field_type is FieldType.MULTI_SELECT and interests.input_type == "checkbox" and [o.value for o in interests.options] == ["backend", "data", "ml"]
    assert interests.required and "interest" in interests.label.lower()
    generic = {f.external_id: f for f in _scan(browser, "generic.html", mode="unknown").fields}
    assert generic["workmode"].field_type is FieldType.RADIO and generic["workmode"].label.startswith("Preferred work mode")
    assert generic["skills"].field_type is FieldType.MULTI_SELECT and generic["skills"].input_type == "select-multiple" or generic["skills"].field_type is FieldType.MULTI_SELECT
    assert generic["alignment"].field_type is FieldType.UNKNOWN and generic["alignment"].required
    assert generic["website"].field_type is FieldType.TEXT


def test_challenge_login_mfa_and_empty_pages_are_detected(browser):
    assert _scan(browser, "captcha.html").captcha
    login = _scan(browser, "login.html")
    assert login.login_wall and not login.captcha
    assert _scan(browser, "mfa.html").mfa
    empty = _scan(browser, "empty.html")
    assert empty.fields == [] and empty.submit_buttons == []
    custom = _scan(browser, "ashby_custom.html")
    assert custom.custom_widgets >= 2 and custom.fields == []


def test_confirmation_and_validation_markers(browser):
    done = _scan(browser, "confirmation.html", ref="ABC-123")
    assert done.success_marker and done.reference == "ABC-123"
    with browser.page() as page:
        page.goto(fixture_url("greenhouse.html"))
        page.click("#submit_app")
        page.wait_for_timeout(200)
        scan = scan_page(page)
    assert scan.validation_errors and "required" in scan.validation_errors[0].lower()


def test_strategy_selection():
    def target(family):
        return ExecutionTarget(source="X", ats_family=family, canonical_url="https://x", company="c", title="t")

    assert strategy_for(target(ATSFamily.GREENHOUSE)) is GREENHOUSE
    assert strategy_for(target(ATSFamily.LEVER)) is LEVER
    assert strategy_for(target(ATSFamily.ASHBY)) is ASHBY and ASHBY.strict_controls
    assert strategy_for(target(ATSFamily.GENERIC_WEB)) is GENERIC


def test_mapping_of_real_greenhouse_form_is_safe(browser):
    scan = _scan(browser, "greenhouse.html")
    bank = [  # (question_key, category, answer)
        ("are you legally authorized to work in this country", "work_authorization", "Yes, I am authorized to work in India."),
        ("will you now or in the future require sponsorship", "sponsorship", "No, I do not require sponsorship."),
    ]
    pkg = _package(
        answers=[("Why are you interested in this role?", "why are you interested in this role", "why_role", "Because ...", "ANSWERED")],
        bank=bank,
        profile={"name": "Ribhu Siripurapu", "first_name": "Ribhu", "last_name": "Siripurapu", "email": "r@example.com"},
    )
    answers = {a.external_id: a for a in map_fields(scan.fields, pkg)}
    assert answers["job_application[first_name]"].answer == "Ribhu" and answers["job_application[last_name]"].answer == "Siripurapu"
    assert answers["job_application[email]"].answer == "r@example.com"
    assert answers["job_application[phone]"].status is FieldAnswerStatus.SKIPPED, "no phone on the profile and it is optional"
    assert answers["job_application[resume]"].status is FieldAnswerStatus.ANSWERED and answers["job_application[resume]"].artifact_type == "RESUME"
    assert answers["job_application[answers_attributes][2][boolean_value]"].selected_values == ["1"], "authorization -> Yes"
    assert answers["job_application[answers_attributes][3][boolean_value]"].selected_values == ["0"], "sponsorship -> No, never confused with authorization"
    assert answers["job_application[answers_attributes][4][boolean_value]"].status is FieldAnswerStatus.SKIPPED
    assert blocking(list(answers.values())) == (0, 0)
    # A required unknown-type field blocks; nothing is guessed.
    unknown = {a.external_id: a for a in map_fields(_scan(browser, "generic.html", mode="unknown").fields, pkg)}
    assert unknown["alignment"].status is FieldAnswerStatus.NEEDS_REVIEW and unknown["alignment"].answer is None


# ---------------------------------------------------------------------------
# Pilot findings on real boards (2026-09-13), reproduced in the fixtures.
# ---------------------------------------------------------------------------


def test_react_select_required_dummy_is_not_a_field(browser):
    """Greenhouse (Discord): every combobox carries an aria-hidden, unfocusable
    "required" dummy input; it was reported as a twin of the real control, so
    each question appeared twice and required counts doubled."""
    scan = _scan(browser, "greenhouse.html")
    country = [f for f in scan.fields if f.label.startswith("Country")]
    assert len(country) == 1 and country[0].external_id == "country"
    assert not any(f.external_id is None and f.label.startswith("Country") for f in scan.fields)


def test_lever_hint_and_status_text_is_not_part_of_the_question(browser):
    """Lever (Spotify): the <label> wraps caption, control, typeahead results
    and upload status; the question must be the caption only, or answer-bank
    keys never match across runs."""
    scan = _scan(browser, "lever.html")
    by_name = {f.external_id: f for f in scan.fields}
    assert by_name["location"].label == "Current location"
    assert by_name["resume"].label == "Resume/CV ✱"
    assert "No location found" not in " ".join(f.label for f in scan.fields)
    assert "Analyzing resume" not in " ".join(f.label for f in scan.fields)
