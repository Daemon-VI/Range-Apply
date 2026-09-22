"""Searchable dropdowns and yes / no buttons are operated, never guessed (2026-09-22).

Every real Greenhouse job-boards form asks for Country and Location (City) in
react-select comboboxes whose options render only after typing; Ashby asks
yes / no questions with two buttons over a hidden checkbox. Until today the
executor refused both, so no Greenhouse application could be completed. The
fixture ``greenhouse_select.html`` reproduces both widgets.
"""

from urllib.parse import unquote

import pytest

from app.career.repository import EvidenceRepository
from app.execution.models import ExecutorKind, FieldAnswerStatus, FieldType
from tests.execution.playwright_conftest import (
    add_bank,
    attach,
    fixture_url,
    make_executor,
    make_session,
    requires_browser,
)

pytestmark = requires_browser

BANK = [
    ("sponsorship", "Will you now or in the future require sponsorship?", "No, I do not require sponsorship."),
    ("consent", "I acknowledge", "Yes"),
    ("other", "Are you able to commit to working from one of our offices on Anchor Days each week?", "Yes"),
]


@pytest.fixture(scope="module")
def browser():
    session = make_session()
    yield session
    session.close()


@pytest.fixture
def resume_file(tmp_path):
    path = tmp_path / "resume.txt"
    path.write_text("Ribhu Siripurapu - resume (test file)\n", encoding="utf-8")
    return path


def _by_prefix(fields):
    """Fixture labels carry the required marker ("Country *"): look fields up by prefix."""

    def find(prefix):
        return next(f for f in fields if f.label.startswith(prefix))

    return find


def _prepared(harness, db_session, tenant_id, company):
    repo = EvidenceRepository(db_session, tenant_id)
    repo.upsert_profile({"location": "Hyderabad, Telangana, India", "degree": "B.Tech in Computer Science and Engineering"}, None, "test")
    db_session.commit()
    add_bank(db_session, tenant_id, BANK)
    return harness.ready(company=company, source="GREENHOUSE", application_url=fixture_url("greenhouse_select.html"))


def test_dry_run_maps_and_sets_every_dropdown_without_pressing_submit(browser, harness, db_session, tenant_id, resume_file):
    executor = make_executor(browser, resume_file, dry_run=True)
    attach(harness, executor)
    attempt = _prepared(harness, db_session, tenant_id, "Acme Select")
    outcome = harness.execute(attempt, worker="sel1", executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert outcome["outcome"] == "DRY_RUN", outcome
    assert executor.submit_clicks == 0

    snapshot = harness.service._latest_snapshot(attempt.id)
    field = _by_prefix(snapshot.fields)
    assert field("Country").field_type == FieldType.COMBOBOX.value and field("Country").answer == "India"
    assert field("Location (City)").answer == "Hyderabad, Telangana, India" and field("Location (City)").selected_values == []
    assert field("Will you now or in the future require sponsorship").status == FieldAnswerStatus.ANSWERED.value
    anchor = field("Are you able to commit")
    assert anchor.field_type == FieldType.YESNO.value and anchor.selected_values == ["Yes"]
    assert field("I acknowledge that I have read").selected_values == ["1"]

    run = harness.service.runs_for(attempt.id)[0]
    filled = run.diagnostics["filled"]
    for prefix in ("Country", "Location (City)", "Will you now or in the future require sponsorship", "Degree", "Are you able to commit", "I acknowledge that I have read"):
        assert any(label.startswith(prefix) for label in filled), (prefix, filled, run.diagnostics.get("unfilled_notes"))
    assert run.diagnostics.get("unfilled_notes") in (None, {})


def test_live_run_submits_with_the_chosen_options(browser, harness, db_session, tenant_id, resume_file):
    executor = make_executor(browser, resume_file, dry_run=False)
    attach(harness, executor)
    attempt = _prepared(harness, db_session, tenant_id, "Acme Select Live")
    outcome = harness.execute(attempt, worker="sel2", executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert outcome["outcome"] == "SUBMITTED", outcome
    assert executor.submit_clicks == 1
    run = harness.service.runs_for(attempt.id)[0]
    result_url = unquote(run.diagnostics["result_url"])
    # The confirmation URL carries what the page itself recorded as chosen.
    assert "country=India;" in result_url and "location=Hyderabad, Telangana, India;" in result_url, result_url
    assert "sponsor=No;" in result_url and "degree=Bachelor's Degree;" in result_url and "anchor=yes" in result_url, result_url


def test_an_answer_no_option_names_leaves_the_field_empty_and_stops(browser, harness, db_session, tenant_id, resume_file):
    executor = make_executor(browser, resume_file, dry_run=False)
    attach(harness, executor)
    repo = EvidenceRepository(db_session, tenant_id)
    # A location the dropdown does not offer.
    repo.upsert_profile({"location": "Atlantis, Nowhere"}, None, "test")
    db_session.commit()
    add_bank(db_session, tenant_id, BANK)
    attempt = harness.ready(company="Acme Select Nowhere", source="GREENHOUSE", application_url=fixture_url("greenhouse_select.html"))
    outcome = harness.execute(attempt, worker="sel3", executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert outcome["outcome"] == "HANDOFF", outcome
    assert executor.submit_clicks == 0
    run = harness.service.runs_for(attempt.id)[0]
    unfillable = run.diagnostics["unfillable_required"]
    assert any(label.startswith("Location (City)") for label in unfillable), unfillable
    note = next(v for k, v in run.diagnostics["unfilled_notes"].items() if k.startswith("Location (City)"))
    assert "no option named" in note
