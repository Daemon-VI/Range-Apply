"""UI redesign (2026-09-14): design system, workflow navigation, safety banner,
command-center data, application grouping, the pre-flight checklist and the
plain-language helpers — through TestClient, against the same fixtures as
``test_control_center.py``.
"""

import re
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.application.models import ApplicationStatus
from app.config import settings
from app.desktop import views
from app.execution.database.models import ExecutionRunRow
from app.execution.models import ExecutorKind, FieldType, FormField, FormSnapshot
from app.main import app
from app.security import DASHBOARD_COOKIE
from tests.execution.conftest import harness  # noqa: F401
from tests.scheduler.conftest import (  # noqa: F401
    answered_bank,
    db_session,
    evidence,
    fake_attempt,
    jobs,
    matches,
    opportunities,
    other_tenant_id,
    scheduler,
    set_policy,
    tenant_id,
)

COOKIE = {DASHBOARD_COOKIE: settings.api_key}
PAGES = ("/desktop/", "/desktop/opportunities", "/desktop/applications", "/desktop/attention", "/desktop/documents", "/desktop/notifications", "/desktop/system")


@pytest.fixture
def client(tenant_id):  # noqa: F811
    app.dependency_overrides[get_tenant_id] = lambda: tenant_id
    try:
        yield TestClient(app, follow_redirects=False, cookies=COOKIE)
    finally:
        app.dependency_overrides.pop(get_tenant_id, None)


@pytest.fixture
def ready(harness):  # noqa: F811
    return harness.ready(company="Desktop Corp", title="Platform Engineer", fit_score=88)


# ------------------------------------------------------------ design system


def test_shared_stylesheet_is_served_offline_and_pages_carry_no_inline_style_or_cdn(client, ready):
    css = TestClient(app).get("/desktop/static/careeros.css")
    assert css.status_code == 200 and "text/css" in css.headers["content-type"]
    for token in ("--ok:", "--warn:", "--danger:", "--uncertain:", "--info:", "--neutral:", ":focus-visible", ".btn.danger", ".empty", ".htmx-indicator"):
        assert token in css.text, token
    for path in PAGES + (f"/desktop/applications/{ready.id}", f"/desktop/applications/{ready.id}/confirm?mode=live"):
        html = client.get(path).text
        assert '<link rel="stylesheet" href="/desktop/static/careeros.css">' in html, path
        assert "<style" not in html, path
        assert "cdnjs" not in html and "https://cdn" not in html, path


def test_every_page_shows_the_safety_banner_with_an_explanation_and_landmarks(client, ready):
    for path in PAGES + (f"/desktop/applications/{ready.id}",):
        html = client.get(path).text
        assert 'class="safety-banner safe"' in html, path
        assert "SAFE / DRY RUN" in html and "Real submission is switched off on the server" in html and "Enable live submission" in html, path
        assert '<main id="main"' in html and '<nav class="sidebar" aria-label="Main">' in html and '<header class="app-header">' in html, path
        assert 'id="toasts" role="status" aria-live="polite"' in html, path


def test_live_mode_banner_is_unmistakable(client, ready, monkeypatch):
    # A config default is not the switch: PLAYWRIGHT_DRY_RUN=false alone still shows SAFE.
    monkeypatch.setattr(settings, "playwright_dry_run", False)
    assert 'class="safety-banner safe"' in client.get("/desktop/").text
    assert client.post("/desktop/api/submission-mode", json={"mode": "live"}, headers={"X-Requested-With": "careeros-desktop"}).status_code == 200
    html = client.get("/desktop/").text
    assert 'class="safety-banner live"' in html and "LIVE SUBMISSION ENABLED" in html
    assert "after typing SUBMIT" in html and "Return to SAFE" in html


def test_navigation_follows_the_workflow_and_marks_the_current_page(client, ready):
    html = client.get("/desktop/applications").text
    labels = [label for label in re.findall(r'class="nav-group-label"[^>]*>([^<]+)<', html)]
    assert labels == ["Job search", "More"]
    assert '<a href="/desktop/applications" aria-current="page">Applications</a>' in html
    for href in ("/dashboard/profile/", "/dashboard/signals", "/dashboard/learning", "/dashboard/ops", "/dashboard/scheduler", "/dashboard/policy", "/desktop/notifications", "/desktop/system"):
        assert f'href="{href}"' in html, href


# ------------------------------------------------------------------- home


def test_home_primary_action_and_pipeline_use_real_counts(client, ready):
    html = client.get("/desktop/").text
    assert "1 application ready — Platform Engineer at Desktop Corp" in html
    assert f'href="/desktop/applications/{ready.id}">Review &amp; Dry Run' in html
    assert re.search(r'<span class="stage-label">Ready</span><div class="stage-value">1</div>', html)
    assert re.search(r'<span class="stage-label">Submitted</span><div class="stage-value">0</div>', html)


def test_home_says_all_caught_up_when_nothing_waits(client, ready, harness):  # noqa: F811
    ready.status = ApplicationStatus.CANCELLED.value
    harness.service.db.commit()
    html = client.get("/desktop/").text
    assert "You're all caught up." in html or "You&#39;re all caught up." in html


# ----------------------------------------------------------- applications


def test_preparing_attempt_whose_package_waits_for_answers_is_grouped_as_needs_input(client, ready, harness):  # noqa: F811
    from app.preparation.database.models import ApplicationPreparationRow

    db = harness.service.db
    ready.status = "PREPARING"
    db.get(ApplicationPreparationRow, ready.preparation_id).status = "NEEDS_USER_INPUT"
    db.commit()
    counts = views._application_group_counts(db, ready.tenant_id)
    assert counts["NEEDS_INPUT"] == 1 and counts["IN_PROGRESS"] == 0 and counts["READY"] == 0
    assert "Desktop Corp" in client.get("/desktop/partials/applications?group=NEEDS_INPUT").text
    assert "Desktop Corp" not in client.get("/desktop/partials/applications?group=IN_PROGRESS").text
    page = client.get("/desktop/applications").text
    assert "Needs your input" in page and f'href="/desktop/applications/{ready.id}#questions">Answer<' in page


def test_ready_row_offers_review_and_dry_run_and_cancel_is_a_confirmed_danger_button(client, ready):
    html = client.get("/desktop/applications?group=READY").text
    assert f'href="/desktop/applications/{ready.id}/confirm?mode=dry_run">Dry run<' in html
    assert f"/desktop/applications/{ready.id}/confirm?mode=live" not in html, "no submit shortcut before a dry run exists"
    cancel = html.split(f'data-url="/api/v1/execution/attempts/{ready.id}/cancel"')[0].rsplit("<button", 1)[1]
    assert 'class="danger sm"' in cancel
    assert "data-confirm=" in html.split(f'data-url="/api/v1/execution/attempts/{ready.id}/cancel"')[1].split(">")[0]


def test_completed_group_is_collapsed(client, ready, harness):  # noqa: F811
    ready.status = ApplicationStatus.CANCELLED.value
    harness.service.db.commit()
    html = client.get("/desktop/applications").text
    assert '<details class="group-collapsed">' in html and "Completed" in html


# ----------------------------------------------------------------- review


def _dry_run(db, attempt, **diagnostics) -> ExecutionRunRow:
    now = datetime.now()
    row = ExecutionRunRow(tenant_id=attempt.tenant_id, application_id=attempt.id, preparation_id=attempt.preparation_id, executor_kind="PLAYWRIGHT_LOCAL", executor_version="test", worker_id="desktop-1", idempotency_key=uuid.uuid4().hex, run_number=1, status="DRY_RUN", outcome="DRY_RUN", submit_invoked=False, started_at=now - timedelta(seconds=20), finished_at=now, diagnostics={"fields_filled": 3, "fields_skipped": 1, **diagnostics})
    db.add(row)
    db.commit()
    return row


def _check(html: str, name: str) -> str:
    part = html.split(f'<span class="check-name">{name}</span>')[1]
    return re.search(r'<span class="check-state (\w+)">([^<]+)</span>', part).group(2)


def test_preflight_checklist_is_derived_from_state_and_honest_about_unknowns(client, ready, harness):  # noqa: F811
    html = client.get(f"/desktop/applications/{ready.id}").text
    assert "Pre-flight checklist" in html
    assert _check(html, "Eligible") == "Yes"
    assert _check(html, "Prepared") == "Yes"
    assert _check(html, "Dry run complete") == "Not yet"
    assert _check(html, "CAPTCHA not encountered") == "Not yet"
    assert _check(html, "Required fields mapped") == "Not yet"
    _dry_run(harness.service.db, ready, captcha_invisible=True)
    html = client.get(f"/desktop/applications/{ready.id}").text
    assert _check(html, "Dry run complete") == "Yes"
    assert _check(html, "CAPTCHA not encountered") == "Invisible CAPTCHA"
    # exactly one primary action; the real submission stays a separate danger step
    assert html.count('class="btn primary lg"') == 1 and ">Run dry run<" in html
    assert f'class="btn danger" href="/desktop/applications/{ready.id}/confirm?mode=live"' in html


def test_review_explains_why_in_plain_language_with_raw_codes_under_details(client, ready):
    html = client.get(f"/desktop/applications/{ready.id}").text
    why = html.split('id="why-title"')[1].split("<details")[0]
    assert "Fit score 88 of 100" in why and "Application policy:" in why
    assert "Reason codes" not in why
    assert "Details and reason codes" in html


def test_form_table_abbreviates_email_and_keeps_answer_route(client, ready, harness):  # noqa: F811
    form = FormSnapshot(source_url="https://example.test/apply", fields=[FormField(external_id="email", label="Email", field_type=FieldType.EMAIL, required=True)], executor_kind=ExecutorKind.MOCK, executor_version="test", metadata={})
    harness.service.capture_form(ready, form)
    harness.service.db.commit()
    html = client.get(f"/desktop/applications/{ready.id}").text
    assert "rib•••@example.com" in html
    assert '<span data-full hidden>ribhu@example.com</span>' in html and 'data-reveal aria-expanded="false"' in html
    assert _check(html, "Required fields mapped") == "Yes"


def test_confirm_page_keeps_every_live_safety_element(client, ready):
    safe = client.get(f"/desktop/applications/{ready.id}/confirm?mode=live").text
    assert 'id="live-disabled"' in safe and 'id="confirm-live"' not in safe
    assert client.post("/desktop/api/submission-mode", json={"mode": "live"}, headers={"X-Requested-With": "careeros-desktop"}).status_code == 200
    html = client.get(f"/desktop/applications/{ready.id}/confirm?mode=live").text
    assert 'id="run-form" onsubmit="return false"' in html and '<input type="hidden" name="mode" value="live">' in html
    assert 'id="confirm-live" class="danger-solid lg" disabled' in html
    assert 'data-require-input="confirm-phrase" data-require-value="SUBMIT"' in html
    assert "Pressing Enter does nothing." in html


# ------------------------------------------------------------ opportunities


def test_opportunities_paginate_and_filter_by_policy_outcome(client, ready, opportunities):  # noqa: F811
    opportunities.make(title="Data Analyst", company="Zed Analytics", fit_score=40, eligibility="UNCERTAIN", admitted=False)
    first = client.get("/desktop/partials/opportunities?limit=1").text
    assert "Showing 1–1 of 2" in first and "page=2" in first and "Next &rarr;" in first
    second = client.get("/desktop/partials/opportunities?limit=1&page=2").text
    assert "Showing 2–2 of 2" in second and "&larr; Previous" in second
    admitted = client.get("/desktop/partials/opportunities?reason=admitted").text
    assert "Desktop Corp" in admitted and "Zed Analytics" not in admitted
    page = client.get("/desktop/opportunities").text
    for label in ("Search", "Location", "Policy outcome", "Eligibility", "Fit band", "Admitted", "State", "Min priority"):
        assert f"<span>{label}</span>" in page, label


# -------------------------------------------------------------- attention


def test_attention_is_a_task_inbox_grouped_by_what_happened(client, ready, harness, opportunities, db_session):  # noqa: F811
    blocked = fake_attempt(db_session, harness.tenant_id, opportunities.make(title="Frontend Engineer", company="Captcha Co"), status=ApplicationStatus.BLOCKED)
    blocked.blocked_reason = "CAPTCHA_REQUIRED"
    fake_attempt(db_session, harness.tenant_id, opportunities.make(title="SRE", company="Failed Ltd"), status=ApplicationStatus.FAILED)
    fake_attempt(db_session, harness.tenant_id, opportunities.make(title="QA", company="Unknown Inc"), status=ApplicationStatus.UNCERTAIN)
    db_session.commit()
    html = client.get("/desktop/attention").text
    blocked_section = html.split('id="bucket-blocked"')[1].split("</section>")[0]
    failed_section = html.split('id="bucket-failed"')[1].split("</section>")[0]
    uncertain_section = html.split('id="bucket-uncertain"')[1].split("</section>")[0]
    assert "Captcha Co" in blocked_section and "Failed Ltd" in failed_section and "Unknown Inc" in uncertain_section
    assert 'id="bucket-needs_you"' not in html, "an empty bucket renders nothing"


def test_package_waiting_for_answers_of_a_cancelled_application_is_stale_not_attention(client, ready, harness):  # noqa: F811
    from app.preparation.database.models import ApplicationPreparationRow

    db = harness.service.db
    db.get(ApplicationPreparationRow, ready.preparation_id).status = "NEEDS_USER_INPUT"
    ready.status = "PREPARING"
    db.commit()
    assert views._attention_count(db, ready.tenant_id) == 1, "an open application still needs you"
    opportunities_html = client.get("/desktop/partials/opportunities").text
    assert f'href="/desktop/applications/{ready.id}#questions">Answer questions<' in opportunities_html
    ready.status = ApplicationStatus.CANCELLED.value
    db.commit()
    assert views._attention_count(db, ready.tenant_id) == 0
    html = client.get("/desktop/attention").text
    assert "Nothing needs you right now." in html
    assert "1 stale package(s) with open questions" in html and "Desktop Corp" in html


# ------------------------------------------------------------------ helpers


def test_mask_value_abbreviates_contact_details_only():
    assert views.mask_value("ribhu@example.com", "email", "Email") == ("rib•••@example.com", True)
    assert views.mask_value("+91-8341000886", "phone", "Phone") == ("•••• 886", True)
    assert views.mask_value("https://linkedin.com/in/x", "text", "LinkedIn Profile") == ("https://linkedin.com/in/x", False)
    assert views.mask_value("", "email", "Email") == ("", False)


def test_admission_text_reads_stored_policy_reasons():
    class Row:
        def __init__(self, reason, admitted):
            self.policy_reason, self.policy_admitted = reason, admitted

    assert views.admission_text(Row("admitted:ELIGIBLE:MEDIUM", True)) == "Admitted (eligible, medium)"
    assert views.admission_text(Row("outside_target_geography:EXCLUDED", False)) == "Outside your target locations"
    assert views.admission_text(Row("outside_target_geography:UNCONFIRMED", False)) == "Location not confirmed in your target area"
    assert views.admission_text(Row("irrelevant_role:UNRELATED/sales", False)) == "Role outside your target roles (sales)"
    assert views.admission_text(Row(None, None)) == "Not evaluated yet"


def test_group_of_follows_the_workflow():
    assert views.group_of("PREPARING", "NEEDS_USER_INPUT") == "NEEDS_INPUT"
    assert views.group_of("PREPARING", "READY") == "IN_PROGRESS"
    assert views.group_of("NEEDS_USER_INPUT") == "NEEDS_INPUT"
    assert views.group_of("READY") == "READY"
    assert views.group_of("UNCERTAIN") == "ATTENTION"
    assert views.group_of("VERIFIED") == "SUBMITTED"
    assert views.group_of("CANCELLED") == "COMPLETED"


def test_opportunities_page_makes_the_existing_rematch_obvious(client, ready, db_session):  # noqa: F811
    from app.pipeline.database.models import CandidateOpportunityRow

    co = db_session.query(CandidateOpportunityRow).filter(CandidateOpportunityRow.id == ready.candidate_opportunity_id).first() if getattr(ready, "candidate_opportunity_id", None) else None
    if co is None:
        co = db_session.query(CandidateOpportunityRow).filter(CandidateOpportunityRow.tenant_id == ready.tenant_id).first()
    co.gate_ruleset_version = "tier1-gates-v1"
    db_session.commit()
    html = client.get("/desktop/opportunities").text
    assert 'id="match-freshness"' in html and "Re-run matching" in html and 'data-url="/api/v3/matches/recalculate"' in html
    assert "decided with older eligibility rules" in html and "applications already created, answers and past runs are not changed" in html


# ---------------------------------------------------------- guided journey


def _step(html, key):
    """One journey step's markup (up to the next step), whatever lists it contains."""
    return html.split(f'id="step-{key}"')[1].split('class="journey-step')[0].split("</ol>")[0]


def test_home_walks_the_person_through_the_steps(client, ready):
    html = client.get("/desktop/").text
    assert 'id="journey"' in html and "Your path to applying" in html
    for key in ("profile", "find", "match", "answer", "review", "submit"):
        assert f'id="step-{key}"' in html, key
    profile = _step(html, "profile")
    assert "Needs you" in profile and "Work authorization" in profile and 'href="/dashboard/profile/"' in profile
    find = _step(html, "find")
    assert "Autopilot is off" in find and "/desktop/api/autopilot" not in find
    review = _step(html, "review")
    assert "1 application ready" in review and 'href="/desktop/applications?group=READY"' in review
    submit = _step(html, "submit")
    assert "SAFE / DRY RUN is on" in submit and "type SUBMIT" in submit
    assert 'aria-current="step"' in html


def test_home_offers_autopilot_controls_when_it_runs(client, ready):
    import time
    from types import SimpleNamespace

    status = {"server": {}, "readiness": None, "health": {}, "window": True, "notifications": {"state": "disabled"},
              "worker": {"state": "running", "dry_run": True, "totals": None, "error": None},
              "autopilot": {"state": "running", "totals": {"paused": False, "running_step": None, "next_cycle_at": time.time() + 600, "last_cycle_at": None, "steps": {}}}}
    previous = getattr(app.state, "desktop", None)
    app.state.desktop = SimpleNamespace(status=lambda: status)
    try:
        html = client.get("/desktop/").text
        find = _step(html, "find")
        assert "Autopilot is on" in find and "next check at" in find
        assert 'data-url="/desktop/api/autopilot"' in find and "&#34;run_now&#34;" in find or '"run_now"' in find
        assert ">Pause<" in find
        status["autopilot"]["totals"]["paused"] = True
        find = _step(client.get("/desktop/").text, "find")
        assert "Autopilot is paused" in find and ">Resume<" in find
    finally:
        app.state.desktop = previous
