"""Increment 3: the desktop control-center pages, through TestClient.

Pages are read-only views over the existing services; writes from the pages
go to the existing API routes with the dashboard cookie plus the desktop
header. The fixtures come from the execution harness (a READY attempt with a
real preparation) and the scheduler conftest (a second tenant).
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.application.models import ApplicationStatus
from app.config import settings
from app.execution.models import ExecutorKind, FieldType, FormField, FormSnapshot
from app.main import app
from app.security import DASHBOARD_COOKIE, DESKTOP_HEADER, DESKTOP_HEADER_VALUE
from tests.execution.conftest import harness  # noqa: F401
from tests.preparation.conftest import OpportunityFactory
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
DESKTOP = {DESKTOP_HEADER: DESKTOP_HEADER_VALUE}


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


# ------------------------------------------------------------------ auth


def test_every_desktop_page_requires_dashboard_auth_and_static_is_open():
    anonymous = TestClient(app, follow_redirects=False)
    for path in ("/desktop/", "/desktop/opportunities", "/desktop/applications", "/desktop/attention", "/desktop/documents", "/desktop/system", "/desktop/partials/opportunities", "/desktop/partials/applications", "/desktop/partials/attention"):
        assert anonymous.get(path).status_code == 401, path
    unguarded = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/desktop/") or path in ("/desktop/login",) or path.startswith("/desktop/static"):
            continue
        deps = " ".join(str(d.call) for d in getattr(route, "dependant", None).dependencies) if hasattr(route, "dependant") else ""
        # JSON / write routes under /desktop/api/ carry require_api_key instead:
        # it accepts the same dashboard cookie plus the desktop header, and the
        # X-API-Key path as well (Increment 4).
        guards = ("require_dashboard_auth", "require_api_key") if path.startswith("/desktop/api/") else ("require_dashboard_auth",)
        if not any(guard in deps for guard in guards):
            unguarded.append(path)
    assert unguarded == [], unguarded
    assert anonymous.get("/desktop/static/desktop.js").status_code == 200
    assert anonymous.get("/desktop/static/htmx.min.js").status_code == 200


def test_pages_and_scripts_carry_no_api_key(client, ready):
    for path in ("/desktop/", "/desktop/opportunities", "/desktop/applications", f"/desktop/applications/{ready.id}", "/desktop/attention", "/desktop/documents", "/desktop/system", "/desktop/static/desktop.js"):
        body = client.get(path).text
        assert settings.api_key not in body, path
        assert "X-API-Key" not in body, path


# ------------------------------------------------------------------ home


def test_home_loads_with_real_counts_and_safe_mode(client, ready):
    page = client.get("/desktop/")
    assert page.status_code == 200
    html = page.text
    assert "SAFE / DRY RUN" in html and "LIVE MODE" not in html
    assert "Discovered" in html and "Your path to applying" in html and "Review and dry run" in html and "Needs attention" in html
    assert "Desktop Corp" not in html or "Recent submissions" in html  # nothing submitted yet: no recent submission rows
    assert "Queue" in html and "Services" in html


# --------------------------------------------------------- opportunities


def test_opportunity_list_search_and_filters(client, ready, opportunities):  # noqa: F811
    other = opportunities.make(title="Data Analyst", company="Zed Analytics", fit_score=40, eligibility="UNCERTAIN", admitted=False)
    page = client.get("/desktop/opportunities")
    assert page.status_code == 200
    assert "Desktop Corp" in page.text and "Zed Analytics" in page.text
    for col in ("Role", "Company", "Location", "Relevance", "Fit", "Eligibility", "Priority", "Status", "Next action"):
        assert f"<th>{col}</th>" in page.text
    searched = client.get("/desktop/partials/opportunities?q=zed").text
    assert "Zed Analytics" in searched and "Desktop Corp" not in searched
    assert "Desktop Corp" in client.get("/desktop/partials/opportunities?q=platform").text
    assert "Nothing contains" in client.get("/desktop/partials/opportunities?q=nomatchatall").text
    assert "Zed Analytics" not in client.get("/desktop/partials/opportunities?eligibility=ELIGIBLE").text
    assert "Zed Analytics" in client.get("/desktop/partials/opportunities?eligibility=UNCERTAIN").text
    assert "Zed Analytics" not in client.get("/desktop/partials/opportunities?admitted=true").text
    assert "Desktop Corp" not in client.get("/desktop/partials/opportunities?fit_band=LOW").text
    assert "Desktop Corp" in client.get("/desktop/partials/opportunities?fit_band=HIGH").text
    assert "never a cut-off" in page.text
    # the API gained the same search parameter; nothing else changed
    api = client.get("/api/v1/opportunities/?search=zed").json()
    assert api["total"] == 1 and api["items"][0]["company"].startswith("Zed Analytics")
    # an opportunity with an attempt redirects to its review page; one without renders the review page itself
    assert client.get(f"/desktop/opportunities/{ready.candidate_opportunity_id}").status_code == 303
    detail = client.get(f"/desktop/opportunities/{other.id}")
    assert detail.status_code == 200 and "Why this job?" in detail.text and "Nothing is prepared" in detail.text


# ---------------------------------------------------------- applications


def test_application_list_groups_and_actions(client, ready, harness):  # noqa: F811
    page = client.get("/desktop/applications")
    assert page.status_code == 200 and "Desktop Corp" in page.text and "Platform Engineer" in page.text
    for group in ("NEEDS_INPUT", "READY", "IN_PROGRESS", "ATTENTION", "SUBMITTED", "COMPLETED"):
        assert f"?group={group}" in page.text
    # pre-redesign group links keep working as status filters
    assert "Desktop Corp" in client.get("/desktop/partials/applications?group=READY").text
    assert "Desktop Corp" in client.get("/desktop/partials/applications?group=READY").text
    assert "Desktop Corp" not in client.get("/desktop/partials/applications?group=FAILED").text
    assert f'data-url="/api/v1/execution/attempts/{ready.id}/cancel"' in page.text and 'data-confirm=' in page.text
    assert f'href="/desktop/applications/{ready.id}"' in page.text


# ---------------------------------------------------------------- review


def test_review_page_answers_why_and_what(client, ready, harness):  # noqa: F811
    page = client.get(f"/desktop/applications/{ready.id}")
    assert page.status_code == 200
    html = page.text
    # why this job
    assert "Why this job?" in html and "Eligibility" in html and "Reason codes" in html and "Admission" in html
    assert "HIGH" in html and "88" in html and "admitted" in html
    assert "Relevant evidence" in html and "CONFIRMED" in html
    # what will be submitted
    assert "What will be submitted?" in html and "Preparation" in html and "v1" in html
    assert "Prepared answers" in html and "ANSWERED" in html
    assert "Inspect execution" in html and "Inspect audit" in html and "Open in browser" in html
    assert "SAFE / DRY RUN" in html and "Real submission happens only from the confirmation screen" in html
    # documents appear with their hash once rendered
    prep = harness.service.db.get(type(harness.service.require_attempt(ready.id)), ready.id)
    report = harness.service.documents.ensure_for_execution(harness.service.db.get(__import__("app.preparation.database.models", fromlist=["ApplicationPreparationRow"]).ApplicationPreparationRow, prep.preparation_id))
    harness.service.db.commit()
    html = client.get(f"/desktop/applications/{ready.id}").text
    for artifact in report.artifacts:
        assert artifact.content_hash[:12] in html and artifact.artifact_type in html


def test_review_shows_needs_user_input_and_answer_form_for_missing_required_field(client, ready, harness):  # noqa: F811
    form = FormSnapshot(source_url="https://example.test/apply", fields=[FormField(external_id="email", label="Email", field_type=FieldType.EMAIL, required=True), FormField(external_id="years_x", label="Years of experience with Kubernetes", field_type=FieldType.TEXT, required=True)], executor_kind=ExecutorKind.MOCK, executor_version="test", metadata={})
    snapshot, answers = harness.service.capture_form(ready, form)
    harness.service.db.commit()
    missing = [a for a in answers if a.status != "ANSWERED"]
    assert missing and missing[0].label.startswith("Years of experience")
    html = client.get(f"/desktop/applications/{ready.id}").text
    assert "NEEDS USER INPUT" in html and "Years of experience with Kubernetes" in html
    assert f'data-url="/api/v1/execution/fields/{missing[0].field_id}/answer"' in html and 'data-method="PUT"' in html
    # nothing was filled in for the person
    assert "ribhu@example.com" in html  # the profile email is mapped
    assert "Kubernetes</td>" not in html.replace("\n", "")  # no invented answer for the unknown field


def test_review_of_another_tenants_attempt_is_not_found(client, db_session, other_tenant_id, jobs, matches):  # noqa: F811
    foreign = OpportunityFactory(db_session, other_tenant_id, jobs, matches).make(title="Secret Role", company="Other Tenant Inc", fit_score=95)
    attempt = fake_attempt(db_session, other_tenant_id, foreign)
    db_session.commit()
    assert client.get(f"/desktop/applications/{attempt.id}").status_code == 404
    assert client.get(f"/desktop/opportunities/{foreign.id}").status_code == 404
    assert "Other Tenant Inc" not in client.get("/desktop/opportunities").text
    assert "Other Tenant Inc" not in client.get("/desktop/applications").text
    assert "Other Tenant Inc" not in client.get("/desktop/attention").text


# ------------------------------------------------------------- attention


def test_attention_center_explains_each_kind_of_intervention(client, ready, harness, opportunities, db_session):  # noqa: F811
    from app.application.database.models import ApplicationRow

    captcha = opportunities.make(title="Frontend Engineer", company="Captcha Co", fit_score=70)
    blocked = fake_attempt(db_session, harness.tenant_id, captcha, status=ApplicationStatus.BLOCKED)
    blocked.blocked_reason = "CAPTCHA_REQUIRED"
    failed_co = opportunities.make(title="SRE", company="Failed Ltd", fit_score=60)
    failed = fake_attempt(db_session, harness.tenant_id, failed_co, status=ApplicationStatus.FAILED)
    failed.status_reason = "navigation failed"
    db_session.commit()
    html = client.get("/desktop/attention").text
    assert "CAPTCHA_REQUIRED" in html and "No automatic bypass was attempted" in html and "Captcha Co" in html
    assert "What happened" in html and "Why it needs you" in html and "What you can do" in html
    assert "Failed Ltd" in html and f'data-url="/api/v1/execution/attempts/{failed.id}/retry"' in html
    assert "Desktop Corp" not in html, "a READY attempt needs nobody"
    assert client.get("/desktop/partials/attention").status_code == 200
    # the sidebar counter reflects the same items
    assert 'class="badge badge-orange">2</span>' in client.get("/desktop/").text
    db_session.query(ApplicationRow).filter(ApplicationRow.id.in_([blocked.id, failed.id])).delete(synchronize_session=False)
    db_session.commit()
    assert "Nothing needs you right now" in client.get("/desktop/attention").text


# ------------------------------------------------------------- documents


def test_documents_page_lists_rendered_artifacts_and_serves_files(client, ready, harness):  # noqa: F811
    from app.preparation.database.models import ApplicationPreparationRow

    assert "No rendered documents yet" in client.get("/desktop/documents").text
    prep = harness.service.db.get(ApplicationPreparationRow, ready.preparation_id)
    report = harness.service.documents.ensure_for_execution(prep)
    harness.service.db.commit()
    html = client.get("/desktop/documents").text
    assert "RESUME" in html and "PASSED" in html and "ACTIVE" in html
    for artifact in report.artifacts:
        assert artifact.content_hash[:12] in html and f'data-url="/api/v1/documents/{artifact.id}/regenerate"' in html
    assert "Desktop Corp" in html
    filtered = client.get(f"/desktop/documents?preparation_id={prep.id}").text
    assert "RESUME" in filtered
    assert "No rendered documents yet" in client.get(f"/desktop/documents?preparation_id={uuid.uuid4().hex}").text
    first = report.artifacts[0]
    served = client.get(f"/desktop/documents/{first.id}/file")
    assert served.status_code == 200 and served.headers["content-type"].startswith("application/pdf")
    assert served.headers["content-disposition"].startswith("inline") and served.headers["x-content-sha256"] == first.content_hash
    assert served.content[:4] == b"%PDF"
    assert TestClient(app, follow_redirects=False).get(f"/desktop/documents/{first.id}/file").status_code == 401


# ---------------------------------------------------------------- system


def test_system_page_exposes_diagnostics_without_secrets(client, ready):
    html = client.get("/desktop/system").text
    for label in ("Queue depth", "Stale leases", "Running attempts", "Execution runs", "Throughput", "Review signals", "Discovery runs", "Failing sources", "AI counters", "Caps", "Kill switches", "Learning snapshot"):
        assert label in html, label
    assert "SAFE / DRY RUN" in html and "Database" in html and "Worker" in html and "Browser execution" in html
    assert settings.api_key not in html and "configured" in html


# ----------------------------------------------------------- write actions


def test_desktop_write_actions_use_cookie_plus_header_and_existing_routes(client, ready, harness):  # noqa: F811
    script = client.get("/desktop/static/desktop.js").text
    assert "'X-Requested-With': 'careeros-desktop'" in script and "credentials: 'same-origin'" in script
    assert "X-API-Key" not in script
    url = f"/api/v1/execution/attempts/{ready.id}/cancel"
    # the browser sends the cookie; without the header the write is refused
    assert client.post(url, json={"reason": "no header"}).status_code == 401
    # cookie + header (what desktop.js sends) reaches the existing route
    response = client.post(url, json={"reason": "cancelled from the desktop"}, headers=DESKTOP)
    assert response.status_code == 200 and response.json()["status"] == "CANCELLED"
    harness.refresh(ready)
    assert ready.status == "CANCELLED"
    assert "CANCELLED" in client.get("/desktop/partials/applications?group=CANCELLED").text
    # the API-key path is untouched
    other = harness.ready(company="Key Corp", title="Backend Engineer")
    keyed = TestClient(app, follow_redirects=False).post(f"/api/v1/execution/attempts/{other.id}/cancel", json={}, headers={"X-API-Key": settings.api_key})
    assert keyed.status_code == 200


# ------------------------------------- Increment 4: the confirmation screen


def test_review_offers_dry_run_and_a_separate_danger_submit_for_ready_attempts(client, ready, db_session, opportunities):  # noqa: F811
    html = client.get(f"/desktop/applications/{ready.id}").text
    assert f'href="/desktop/applications/{ready.id}/confirm?mode=dry_run"' in html
    assert f'class="btn danger" href="/desktop/applications/{ready.id}/confirm?mode=live"' in html
    assert ">Run dry run<" in html and ">Submit for real<" in html
    assert "Real submission happens only from the confirmation screen, one application at a time, in a visible browser." in html
    assert "Real submission is not available" not in html
    # an attempt that is not READY (and has no preparation) offers neither button
    blocked = fake_attempt(db_session, ready.tenant_id, opportunities.make(title="Blocked Role", company="Blocked Ltd"), status=ApplicationStatus.BLOCKED)
    db_session.commit()
    other = client.get(f"/desktop/applications/{blocked.id}").text
    assert f"/desktop/applications/{blocked.id}/confirm" not in other


def test_confirm_page_dry_run_shows_the_whole_package_and_never_presses_submit(client, ready, harness):  # noqa: F811
    page = client.get(f"/desktop/applications/{ready.id}/confirm?mode=dry_run")
    assert page.status_code == 200
    html = page.text
    assert "Desktop Corp" in html and "Platform Engineer" in html and ready.id in html
    assert "attempt #1" in html
    assert "Target URL" in html and "Open in browser" in html and "https://" in html
    assert "HEADED PLAYWRIGHT" in html and "a visible Chromium window on this machine" in html
    assert "The submit control is never pressed in a dry run." in html
    assert "Pre-submit preconditions" in html
    assert "Prepared answers" in html and "ANSWERED" in html
    # nothing rendered yet: the page says so instead of inventing a hash
    assert "not rendered yet — rendering happens when execution starts" in html
    assert 'data-body=\'{"mode": "dry_run"}\'' in html and 'data-next-from="job_url"' in html
    assert f'data-url="/desktop/api/attempts/{ready.id}/run"' in html
    assert "This will submit a real job application to the employer." not in html
    assert settings.api_key not in html and "X-API-Key" not in html
    # once the documents exist, the exact artifact type / version / SHA-256 is shown
    from app.preparation.database.models import ApplicationPreparationRow

    report = harness.service.documents.ensure_for_execution(harness.service.db.get(ApplicationPreparationRow, ready.preparation_id))
    harness.service.db.commit()
    html = client.get(f"/desktop/applications/{ready.id}/confirm?mode=dry_run").text
    for artifact in report.artifacts:
        assert artifact.content_hash[:16] in html and f"v{artifact.version}" in html


def test_confirm_page_live_demands_a_typed_phrase_before_a_real_submission(client, ready):
    assert client.post("/desktop/api/submission-mode", json={"mode": "live"}, headers={"X-Requested-With": "careeros-desktop"}).status_code == 200
    html = client.get(f"/desktop/applications/{ready.id}/confirm?mode=live").text
    assert "This will submit a real job application to the employer." in html
    assert 'id="confirm-phrase"' in html and 'autocomplete="off"' in html and 'name="confirm"' in html
    assert 'data-require-input="confirm-phrase"' in html and 'data-require-value="SUBMIT"' in html
    assert 'data-next-from="job_url"' in html and f'data-url="/desktop/api/attempts/{ready.id}/run"' in html
    assert 'data-form="run-form"' in html and 'id="run-form" onsubmit="return false"' in html
    assert '<input type="hidden" name="mode" value="live">' in html
    assert "Submit for real — I understand" in html
    assert settings.api_key not in html and "X-API-Key" not in html
    # the guard is in the script too, and still carries no key
    script = client.get("/desktop/static/desktop.js").text
    assert "data-require-value" in script and "data-require-input" in script and "data-next-from" in script
    assert "X-API-Key" not in script


def test_confirm_page_refuses_an_attempt_that_is_not_ready(client, db_session, tenant_id, opportunities):  # noqa: F811
    attempt = fake_attempt(db_session, tenant_id, opportunities.make(title="Half Done", company="Half Ltd"), status=ApplicationStatus.NEEDS_REVIEW)
    db_session.commit()
    html = client.get(f"/desktop/applications/{attempt.id}/confirm?mode=live").text
    assert "Nothing can be run from here yet." in html
    assert "no preparation attached" in html and "not READY" in html
    assert "data-require-value" not in html and "Start dry run" not in html


def test_confirm_page_of_another_tenants_attempt_is_not_found(client, db_session, other_tenant_id, jobs, matches):  # noqa: F811
    foreign = OpportunityFactory(db_session, other_tenant_id, jobs, matches).make(title="Secret Role", company="Other Tenant Inc", fit_score=95)
    attempt = fake_attempt(db_session, other_tenant_id, foreign)
    db_session.commit()
    assert client.get(f"/desktop/applications/{attempt.id}/confirm?mode=live").status_code == 404


def test_review_offers_an_answer_box_for_each_open_preparation_question_with_reuse_notes(client, ready, harness, db_session):  # noqa: F811
    """First real dry run: the Review page said 'answer below' but offered no box for the
    preparation's own open questions, and did not say whether an answer is reused."""
    from app.preparation.database.models import PreparationAnswerRow

    prep_id = ready.preparation_id
    reusable = PreparationAnswerRow(tenant_id=ready.tenant_id, preparation_id=prep_id, position=90, question="Do you hold a valid permit to work in India?", question_key="do you hold a valid permit to work in india", category="work_authorization", status="NEEDS_USER_INPUT", source="NONE", required=True, reason="work_authorization: not recorded in the Career Brain or answer bank", evidence_keys=[])
    specific = PreparationAnswerRow(tenant_id=ready.tenant_id, preparation_id=prep_id, position=91, question="Why do you want to join Desktop Corp in particular?", question_key="why do you want to join desktop corp in particular", category="why_company", status="NEEDS_USER_INPUT", source="NONE", required=True, reason="needs the candidate", evidence_keys=[])
    db_session.add_all([reusable, specific])
    db_session.commit()
    html = client.get(f"/desktop/applications/{ready.id}").text
    assert f'data-url="/api/v1/preparations/{prep_id}/answers/{reusable.id}"' in html and f'data-url="/api/v1/preparations/{prep_id}/answers/{specific.id}"' in html
    assert 'data-method="PUT"' in html and f'href="/dashboard/preparations/{prep_id}"' in html
    reusable_form = html.split(f'id="prep-answer-{reusable.id}"')[1].split("</form>")[0]
    specific_form = html.split(f'id="prep-answer-{specific.id}"')[1].split("</form>")[0]
    assert 'name="save_to_bank"' in reusable_form and "future applications" in reusable_form
    assert 'name="save_to_bank"' not in specific_form and "Employer-specific" in specific_form
    assert "Why it is asked of you: work_authorization: not recorded" in html
    assert settings.api_key not in html
    # The box writes through the existing route with the desktop credential: cookie + header.
    response = client.put(f"/api/v1/preparations/{prep_id}/answers/{reusable.id}", json={"answer": "Yes, authorized to work in India.", "save_to_bank": False}, headers={DESKTOP_HEADER: DESKTOP_HEADER_VALUE})
    assert response.status_code == 200, response.text
    db_session.refresh(reusable)
    assert reusable.status == "ANSWERED" and reusable.answer == "Yes, authorized to work in India."
    db_session.refresh(specific)
    assert specific.status == "NEEDS_USER_INPUT" and specific.answer is None, "nothing else was filled in"
