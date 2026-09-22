"""After a person clears a CAPTCHA / login wall, the executor must not act on a
form it never mapped.

Audit finding (2026-09-14): a wall page with no form fields was scanned and
mapped (zero answers); the person cleared the challenge in the headed window;
the application form appeared; the executor then filled nothing, re-ran the
gate and pressed submit on the unmapped form. The run now stops before filling
with a retryable FORM_CHANGED failure, so the next run discovers and maps the
real form first.
"""

import pytest

from app.application.models import ApplicationStatus
from app.execution.models import ErrorClass, ExecutionStatus, ExecutorKind
from tests.execution.playwright_conftest import (
    attach,
    make_executor,
    make_session,
    requires_browser,
)

pytestmark = requires_browser

WALL_THEN_FORM = """<!doctype html>
<html><head><meta charset="utf-8"><title>Apply</title></head>
<body>
<div id="wall"><h1>Please verify that you are human</h1></div>
<script>
setTimeout(function () {
  document.getElementById('wall').remove();
  document.body.insertAdjacentHTML('beforeend',
    '<form novalidate><p><label>Email *<input type="email" name="email" required></label></p>' +
    '<p><label>Anything else? *<textarea name="notes" required></textarea></label></p>' +
    '<button type="submit">Submit application</button></form>');
  document.querySelector('form').addEventListener('submit', function (e) {
    e.preventDefault();
    document.body.innerHTML = '<h1>Thank you for applying</h1>';
  });
}, 2500);
</script>
</body></html>
"""


@pytest.fixture(scope="module")
def browser():
    session = make_session()
    yield session
    session.close()


def test_form_that_appears_after_a_cleared_wall_is_never_submitted_unmapped(browser, harness, tmp_path, monkeypatch):
    page = tmp_path / "wall_then_form.html"
    page.write_text(WALL_THEN_FORM, encoding="utf-8")
    resume = tmp_path / "resume.txt"
    resume.write_text("resume", encoding="utf-8")
    executor = make_executor(browser, resume, dry_run=False, settle_ms=0)
    # A visible browser the person works in: the executor waits for the wall to clear.
    executor.handoff_wait_seconds = 15
    monkeypatch.setattr(browser, "headless", False)
    attach(harness, executor)
    attempt = harness.ready(company="Wall Corp", source="CAREERS", application_url=page.resolve().as_uri())

    outcome = harness.execute(attempt, worker="wall", executor=ExecutorKind.PLAYWRIGHT_LOCAL)

    assert executor.submit_clicks == 0, outcome
    run = harness.service.runs_for(attempt.id)[0]
    assert outcome["outcome"] == "RETRYABLE_FAILURE", outcome
    assert run.status == ExecutionStatus.FAILED_RETRYABLE.value and run.error_class == ErrorClass.FORM_CHANGED.value
    assert run.submit_invoked is False
    assert attempt.status == ApplicationStatus.READY.value
