"""A listing page's own "Apply" link is followed once before giving up (2026-09-22).

Real finding: Stripe's job page (and employer career sites in front of
Greenhouse) show the posting with an Apply link; the executor handed off
AMBIGUOUS_FORM ("no application form found") without opening it. Following
a link is navigation, never a submission.
"""

import pytest

from app.execution.models import ExecutorKind, HandoffReason
from tests.execution.playwright_conftest import (
    attach,
    fixture_url,
    make_executor,
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
def resume_file(tmp_path):
    path = tmp_path / "resume.txt"
    path.write_text("Ribhu Siripurapu - resume (test file)\n", encoding="utf-8")
    return path


def test_the_apply_link_of_a_listing_page_is_followed_to_the_form(browser, harness, resume_file):
    executor = make_executor(browser, resume_file, dry_run=True)
    attach(harness, executor)
    attempt = harness.ready(company="Acme Listing", source="GREENHOUSE", application_url=fixture_url("listing_apply.html"))
    outcome = harness.execute(attempt, worker="al1", executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert outcome["outcome"] == "DRY_RUN", outcome
    run = harness.service.runs_for(attempt.id)[0]
    assert run.diagnostics["url"].endswith("greenhouse.html") and run.diagnostics["fields_filled"] >= 4
    assert executor.submit_clicks == 0


def test_a_listing_page_without_an_apply_link_still_hands_off(browser, harness, resume_file):
    executor = make_executor(browser, resume_file, dry_run=True)
    attach(harness, executor)
    attempt = harness.ready(company="Acme Search Only", source="GREENHOUSE", application_url=fixture_url("search_only.html"))
    outcome = harness.execute(attempt, worker="al2", executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert outcome["outcome"] == "HANDOFF" and attempt.blocked_reason == HandoffReason.AMBIGUOUS_FORM.value, outcome
