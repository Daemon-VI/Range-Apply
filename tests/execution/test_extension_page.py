"""The browser extension's page scripts, executed verbatim under Playwright
against the local fixture forms, with the service worker's part played by
:class:`ExtensionDriver` (same ExecutionService calls as the HTTP routes)."""

import pytest

from app.application.models import ApplicationStatus
from app.execution.models import (
    ErrorClass,
    ExecutionStatus,
    ExecutorKind,
    HandoffReason,
    VerificationStatus,
)
from app.pipeline.models import QueueState
from tests.execution.extension_driver import ExtensionDriver
from tests.execution.playwright_conftest import (
    GENERIC_BANK,
    add_bank,
    fixture_url,
    make_session,
    requires_browser,
)

pytestmark = requires_browser


@pytest.fixture(scope="module")
def browser():
    session = make_session()
    yield session
    session.close()


@pytest.fixture
def page(browser, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "documents_root", str(tmp_path / "documents"))
    with browser.page() as page:
        yield page


def _driver(harness, page, **kwargs) -> ExtensionDriver:
    return ExtensionDriver(harness, page, **kwargs)


# ------------------------------------------------------------- submissions


def test_person_presses_submit_after_fill_gated_then_verified(harness, page, db_session):
    attempt = harness.ready(company="Ext Acme", source="GREENHOUSE", application_url=fixture_url("greenhouse.html"))
    ext = _driver(harness, page)
    assert ext.fill(attempt) == "filled"
    assert ext.fill_report["missing_required"] == [] and len(ext.fill_report["filled"]) >= 7
    assert page.input_value("#email") == "ribhu@example.com" and page.input_value("#first_name")
    assert "RESUME" in ext.artifacts_sent and page.evaluate("() => document.querySelector('#resume').files[0].name") == "resume-v1.pdf"
    assert page.evaluate("() => document.querySelector('#resume').files[0].size") > 500
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.SUBMITTING.value
    run = harness.service.runs_for(attempt.id)[0]
    assert run.submit_invoked is False, "nothing recorded before the person acts"

    ext.user_clicks("#submit_app")
    outcome = ext.wait()
    assert ext.gate_calls and ext.gate_calls[0]["ok"] is True
    assert outcome["outcome"] == "SUBMITTED", (outcome, ext.reported)
    harness.refresh(attempt)
    db_session.refresh(run)
    assert run.submit_invoked is True and run.status == ExecutionStatus.VERIFIED.value
    assert run.verification_status == VerificationStatus.VERIFIED.value and run.confirmation_reference.startswith("GH-")
    assert attempt.status == ApplicationStatus.VERIFIED.value and attempt.confirmation.startswith("GH-")
    uploads = {u["type"]: u for u in run.diagnostics["uploads"]}
    assert uploads["RESUME"]["sha256"] == run.diagnostics["artifacts"]["RESUME"]["sha256"] and uploads["RESUME"]["artifact_id"] == run.resume_artifact_id
    assert harness.item(attempt).state == QueueState.SUCCEEDED.value
    assert [m["type"] for m in ext.bridge_log].count("gate") == 1


def test_auto_mode_presses_submit_itself_and_generic_controls_fill(harness, page, db_session):
    add_bank(db_session, harness.tenant_id, GENERIC_BANK)
    attempt = harness.ready(company="Delta", source="CAREERS", application_url=fixture_url("generic.html"))
    ext = _driver(harness, page, mode="auto")
    ext.fill(attempt)
    outcome = ext.wait()
    assert outcome["outcome"] == "SUBMITTED", (outcome, ext.fill_report)
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.VERIFIED.value and attempt.confirmation.startswith("GEN-")
    assert "Preferred work mode *" in ext.fill_report["filled"]


def test_validation_error_after_submit_needs_review(harness, page):
    attempt = harness.ready(company="Ext Invalid", source="GREENHOUSE", application_url=fixture_url("greenhouse.html", mode="validation"))
    ext = _driver(harness, page)
    ext.fill(attempt)
    ext.user_clicks("#submit_app")
    outcome = ext.wait()
    assert outcome["outcome"] == "NEEDS_REVIEW"
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.NEEDS_REVIEW.value
    run = harness.service.runs_for(attempt.id)[0]
    assert run.error_class == ErrorClass.VALIDATION.value and run.submit_invoked is True
    assert "Email is invalid" in run.diagnostics["validation_errors"][0]


def test_ambiguous_post_submit_state_is_uncertain_never_resubmitted(harness, page):
    attempt = harness.ready(company="Ext Hang", source="GREENHOUSE", application_url=fixture_url("greenhouse.html", mode="hang"))
    ext = _driver(harness, page, wait_ms=1000)
    ext.fill(attempt)
    ext.user_clicks("#submit_app")
    outcome = ext.wait()
    assert outcome["outcome"] == "UNKNOWN"
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.UNCERTAIN.value
    run = harness.service.runs_for(attempt.id)[0]
    assert run.status == ExecutionStatus.UNKNOWN.value and run.submit_invoked is True
    assert harness.item(attempt).state == QueueState.NEEDS_REVIEW.value
    # A second click on the same page is not re-gated into a second submission.
    ext.user_clicks("#submit_app")
    page.wait_for_timeout(300)
    assert [m["type"] for m in ext.bridge_log].count("gate") == 1


def test_captcha_after_submit_hands_off(harness, page):
    attempt = harness.ready(company="Ext Captcha2", source="GREENHOUSE", application_url=fixture_url("greenhouse.html", mode="captcha"))
    ext = _driver(harness, page)
    ext.fill(attempt)
    ext.user_clicks("#submit_app")
    outcome = ext.wait()
    assert outcome["outcome"] == "HANDOFF"
    run = harness.service.runs_for(attempt.id)[0]
    assert run.handoff_reason == HandoffReason.CAPTCHA_REQUIRED.value and run.submit_invoked is True


# ------------------------------------------------------- human conditions


@pytest.mark.parametrize("fixture, reason", [("captcha.html", "CAPTCHA_REQUIRED"), ("login.html", "AUTH_REQUIRED"), ("mfa.html", "MFA_REQUIRED"), ("empty.html", "UNSUPPORTED_FORM")])
def test_human_verification_pages_hand_off_before_any_fill(harness, page, fixture, reason):
    attempt = harness.ready(company=f"Ext {reason}", source="CAREERS", application_url=fixture_url(fixture))
    ext = _driver(harness, page)
    assert ext.fill(attempt) == "handoff" and ext.handoff_reason == reason
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.BLOCKED.value and attempt.blocked_reason == reason
    assert page.evaluate("() => Array.from(document.querySelectorAll('input')).filter(i => i.value).length") == 0
    # The person finishes and confirms from the popup.
    run = harness.service.runs_for(attempt.id)[0]
    confirmed = harness.service.confirm(run.id, True, "EXT-REF-1")
    assert confirmed.status == ExecutionStatus.VERIFIED.value
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.VERIFIED.value


def test_unknown_required_field_waits_for_the_person_and_answer_flows_through_the_bank(harness, page, db_session):
    add_bank(db_session, harness.tenant_id, GENERIC_BANK)
    attempt = harness.ready(company="Delta Unknown", source="CAREERS", application_url=fixture_url("generic.html", mode="unknown"))
    ext = _driver(harness, page)
    assert ext.fill(attempt) == "awaiting_user"
    labels = {p["label"]: p["status"] for p in ext.pending}
    assert labels == {"Rate your alignment with our values *": "NEEDS_REVIEW"}
    assert page.input_value('input[name="alignment"]') == "3", "an unknown control is never touched (browser default)"
    # The person's click is intercepted and gated exactly like any other, but
    # the popup keeps the submit button hidden while fields are pending; here
    # the person fills the range themselves and submits from the page.
    page.evaluate("() => { const r = document.querySelector('input[name=\"alignment\"]'); r.value = '4'; }")
    ext.user_clicks("button[type=submit]")
    outcome = ext.wait()
    assert outcome["outcome"] == "SUBMITTED" and ext.gate_calls[0]["ok"]


def test_needs_user_input_field_answered_in_popup_is_saved_and_filled(harness, page, db_session):
    """A required field with no truthful answer: the person answers it (popup),
    the answer goes through fields/{id}/answer and the re-fill lands it."""
    from app.career.repository import EvidenceRepository

    add_bank(db_session, harness.tenant_id, [g for g in GENERIC_BANK if g[0] != "why_company"])
    attempt = harness.ready(company="Delta Ask", source="CAREERS", application_url=fixture_url("generic.html"))
    ext = _driver(harness, page)
    assert ext.fill(attempt) == "awaiting_user"
    pending = {p["label"]: p for p in ext.pending}
    assert "Why do you want to work at Delta? *" in pending
    assert page.input_value('textarea[name="why_company"]') == ""
    field_id = pending["Why do you want to work at Delta? *"]["field_id"]
    assert ext.answer(field_id, "Delta's infrastructure work matches my Ticket Engine project.", save_to_bank=True) == "filled"
    assert page.input_value('textarea[name="why_company"]').startswith("Delta's infrastructure")
    repo = EvidenceRepository(db_session, harness.tenant_id)
    saved = [repo.find_answer(q) for q in ("Why do you want to work at Delta?", "Why do you want to work at Delta? *", "Why do you want to work at Delta")]
    assert any(saved), "the answer went into the bank (through the preparation question or the field label)"


# -------------------------------------------------------------- the gate


def test_gate_refuses_the_click_when_policy_changed(harness, page, db_session):
    from tests.execution.conftest import set_policy

    attempt = harness.ready(company="Ext Gate", source="GREENHOUSE", application_url=fixture_url("greenhouse.html"))
    ext = _driver(harness, page)
    ext.fill(attempt)
    set_policy(db_session, harness.tenant_id, blocked_companies=[harness.opportunities.jobs.company("Ext Gate")])
    ext.user_clicks("#submit_app")
    outcome = ext.wait(timeout_s=3)
    assert ext.gate_calls[0]["ok"] is False and any("COMPANY_BLOCKED" in f for f in ext.gate_calls[0]["failures"])
    assert outcome["outcome"] == "NEEDS_REVIEW"
    assert page.url.endswith("greenhouse.html"), "the click never left the page"
    harness.refresh(attempt)
    run = harness.service.runs_for(attempt.id)[0]
    assert run.submit_invoked is False and run.error_class == ErrorClass.POLICY.value and run.diagnostics["gate_failures"]
    assert "not submitted" in page.text_content("#careeros-banner")


def test_gate_refuses_when_the_kill_switch_is_on(harness, page, db_session):
    from app.application.killswitch import set_paused

    attempt = harness.ready(company="Ext Paused", source="GREENHOUSE", application_url=fixture_url("greenhouse.html"))
    ext = _driver(harness, page)
    ext.fill(attempt)
    set_paused(db_session, True, "maintenance")
    try:
        ext.user_clicks("#submit_app")
        ext.wait(timeout_s=3)
    finally:
        set_paused(db_session, False)
    assert ext.gate_calls[0]["ok"] is False and any("PAUSED" in f for f in ext.gate_calls[0]["failures"])
    assert page.url.endswith("greenhouse.html")


def test_second_gate_call_on_the_same_run_is_refused(harness, page, db_session):
    attempt = harness.ready(company="Ext Twice", source="GREENHOUSE", application_url=fixture_url("greenhouse.html", mode="hang"))
    ext = _driver(harness, page, wait_ms=500)
    ext.fill(attempt)
    ext.user_clicks("#submit_app")
    ext.wait()
    second = harness.service.gate(ext.item, ext.worker) if harness.item(attempt).claimed_by == ext.worker else None
    if second is not None:
        assert second["ok"] is False and "SUBMIT_ALREADY_INVOKED" in second["failures"][0]


# -------------------------------------------------------------- dry run


def test_dry_run_fills_reports_and_blocks_the_submit(harness, page):
    attempt = harness.ready(company="Ext Dry", source="GREENHOUSE", application_url=fixture_url("greenhouse.html"))
    ext = _driver(harness, page, mode="dry_run")
    assert ext.fill(attempt) == "settled" and ext.report_outcome["outcome"] == "DRY_RUN"
    harness.refresh(attempt)
    assert attempt.status == ApplicationStatus.READY.value
    run = harness.service.runs_for(attempt.id)[0]
    assert run.status == ExecutionStatus.DRY_RUN.value and run.submit_invoked is False and run.diagnostics["fields_filled"] >= 7
    ext.user_clicks("#submit_app")
    page.wait_for_timeout(300)
    assert page.url.endswith("greenhouse.html") and "dry-run mode" in page.text_content("#careeros-banner")
    assert [m["type"] for m in ext.bridge_log].count("gate") == 0


# ------------------------------------------------------------- documents


def test_tampered_document_is_not_uploaded_and_hands_off(harness, page):
    attempt = harness.ready(company="Ext Tamper", source="GREENHOUSE", application_url=fixture_url("greenhouse.html"))
    ext = _driver(harness, page)
    # Start first so the document exists, then corrupt it on disk.
    item = harness.item(attempt)
    ext.item = harness.service.claim_item(item, ext.worker)
    run, package, outcome = harness.service.start(ext.item, ext.worker, ExecutorKind.BROWSER_EXTENSION)
    assert outcome["outcome"] == "started"
    ext.run, ext.package = run, package
    path = package.execution_config["artifact_files"]["RESUME"]
    with open(path, "ab") as handle:
        handle.write(b"\n%tampered")
    assert ext.fill(attempt) == "handoff" and ext.handoff_reason == "ARTIFACT_FILE_REQUIRED"
    assert page.evaluate("() => document.querySelector('#resume').files.length") == 0
    harness.refresh(attempt)
    assert attempt.blocked_reason == HandoffReason.ARTIFACT_FILE_REQUIRED.value


# ----------------------------------------------------------- page scripts


@pytest.mark.parametrize("name", ["api.js", "background.js", "content.js", "discover.js", "fill.js", "popup.js", "options.js"])
def test_extension_sources_parse_and_carry_no_secrets(browser, name):
    """No Node toolchain here: the browser's parser is the syntax gate."""
    import re

    from tests.execution.extension_driver import EXTENSION_SRC

    source = (EXTENSION_SRC / name).read_text(encoding="utf-8")
    with browser.page() as page:
        assert page.evaluate("(src) => { new Function(src); return true; }", source) is True
    assert not re.search(r"(api[_-]?key|token|secret)\s*[:=]\s*['\"][A-Za-z0-9]{8,}", source, re.IGNORECASE), "no embedded credential"
    assert "sqlite" not in source.lower() and "postgres" not in source.lower(), "the extension never touches the database"


def test_manifest_is_mv3_local_only_and_framework_free():
    import json

    from tests.execution.extension_driver import EXTENSION_SRC

    manifest = json.loads((EXTENSION_SRC.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3 and manifest["background"]["service_worker"] == "src/background.js"
    assert set(manifest["host_permissions"]) == {"http://127.0.0.1/*", "http://localhost/*"}
    assert "<all_urls>" not in manifest.get("host_permissions", []) and "content_scripts" not in manifest
    assert not list((EXTENSION_SRC.parent).glob("package.json")) and not list((EXTENSION_SRC.parent).glob("node_modules"))


def test_page_scripts_never_read_passwords_or_persist_html(harness, page):
    attempt = harness.ready(company="Ext Login2", source="CAREERS", application_url=fixture_url("login.html"))
    ext = _driver(harness, page)
    ext.fill(attempt)
    scan = ext.scan
    assert scan["login_wall"] is True and all(f.get("input_type") != "password" for f in scan["fields"])
    assert "<" not in scan["body_excerpt"] and len(scan["body_excerpt"]) <= 2000


def test_captcha_widget_completed_by_the_person_is_not_a_wall(harness, page):
    """The extension never solves a challenge; once the person has (the
    provider's response token is present) Fill proceeds (Phase 13, Lever)."""
    attempt = harness.ready(company="Ext Solved", source="CAREERS", application_url=fixture_url("captcha.html", solved=1))
    ext = _driver(harness, page)
    assert ext.fill(attempt) == "filled", ext.handoff_reason
    assert page.input_value('input[name="email"]') == "ribhu@example.com"


def test_invisible_recaptcha_badge_does_not_block_the_extension_fill(harness, page):
    attempt = harness.ready(company="Ext Badge", source="GREENHOUSE", application_url=fixture_url("greenhouse.html", badge=1))
    ext = _driver(harness, page)
    assert ext.fill(attempt) == "filled", ext.handoff_reason
