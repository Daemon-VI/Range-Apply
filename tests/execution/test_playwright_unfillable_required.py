"""A required field the executor cannot set is never submitted empty.

Audit 2026-09-14: once a person "answered" a required field of a type the
executor could not fill (a yes / no button question, a searchable dropdown),
the executor skipped it silently and pressed submit. It now hands off
UNSUPPORTED_FORM before the click, in a dry run and for real.

2026-09-22: yes / no buttons and searchable dropdowns are operated when one
option names the answer; an answer that names no option still stops the run.
"""

import pytest

from app.application.models import ApplicationStatus
from app.execution.models import ExecutionStatus, ExecutorKind, FieldType, HandoffReason
from tests.execution.playwright_conftest import (
    attach,
    make_executor,
    make_session,
    requires_browser,
)

pytestmark = requires_browser

YES_NO_FORM = """<!doctype html>
<html><head><meta charset="utf-8"><title>Apply</title></head>
<body>
<form novalidate>
  <p><label>Email *<input type="email" name="email" required></label></p>
  <div class="entry"><label class="title required" for="q-anchor">Can you work from the office three days a week?</label>
    <div class="yesno"><button type="button" aria-pressed="false">Yes</button><button type="button" aria-pressed="false">No</button>
    <input type="checkbox" name="q-anchor" tabindex="-1" style="position:absolute;opacity:0;width:0;height:0;margin:0;padding:0;border:0"></div></div>
  <button type="submit">Submit application</button>
</form>
<script>
document.querySelectorAll('.yesno button').forEach(function (b) { b.addEventListener('click', function () {
  b.parentElement.querySelectorAll('button').forEach(function (x) { x.setAttribute('aria-pressed', 'false'); });
  b.setAttribute('aria-pressed', 'true'); b.parentElement.querySelector('input').checked = true; }); });
document.querySelector('form').addEventListener('submit', function (e) { e.preventDefault(); document.body.innerHTML = '<h1>Thank you for applying</h1><p>pressed=' + (document.querySelector('[aria-pressed="true"]') || {textContent: 'none'}).textContent + '</p>'; });
</script>
</body></html>
"""


@pytest.fixture(scope="module")
def browser():
    session = make_session()
    yield session
    session.close()


def _first_run(browser, harness, tmp_path, company):
    page = tmp_path / "yes_no.html"
    page.write_text(YES_NO_FORM, encoding="utf-8")
    executor = make_executor(browser, None, dry_run=False)
    attach(harness, executor)
    attempt = harness.ready(company=company, source="CAREERS", application_url=page.resolve().as_uri())
    first = harness.execute(attempt, worker="yn1", executor=ExecutorKind.PLAYWRIGHT_LOCAL)
    assert first["outcome"] == "HANDOFF" and attempt.blocked_reason == HandoffReason.UNKNOWN_REQUIRED_FIELD.value, first
    snapshot = harness.service._latest_snapshot(attempt.id)
    question = next(f for f in snapshot.fields if f.field_type == FieldType.YESNO.value)
    assert question.required and question.label.startswith("Can you work from the office")
    assert [o["label"] for o in question.options] == ["Yes", "No"]
    return executor, attempt, question


def test_an_answer_that_names_no_button_hands_off_before_submit(browser, harness, tmp_path):
    executor, attempt, question = _first_run(browser, harness, tmp_path, "YesNo Corp")

    # The person answers with something neither button says: nothing is pressed, nothing submitted.
    harness.service.answer_field(question.id, "Three days is fine", "test")
    harness.service.retry(attempt.id, "test", "answered")
    second = harness.execute(attempt, worker="yn2", executor=ExecutorKind.PLAYWRIGHT_LOCAL)

    assert executor.submit_clicks == 0, second
    assert second["outcome"] == "HANDOFF" and attempt.status == ApplicationStatus.BLOCKED.value
    assert attempt.blocked_reason == HandoffReason.UNSUPPORTED_FORM.value
    run = harness.service.runs_for(attempt.id)[0]
    assert run.status == ExecutionStatus.HANDOFF.value and run.handoff["stopped_at"] == "fill"
    assert "Can you work from the office" in run.handoff["message"]
    assert run.diagnostics["unfilled_notes"]


def test_a_yes_presses_the_yes_button_and_the_form_submits(browser, harness, tmp_path):
    executor, attempt, question = _first_run(browser, harness, tmp_path, "YesNo Pressed Corp")

    harness.service.answer_field(question.id, "Yes", "test")
    harness.service.retry(attempt.id, "test", "answered")
    second = harness.execute(attempt, worker="yn3", executor=ExecutorKind.PLAYWRIGHT_LOCAL)

    assert second["outcome"] == "SUBMITTED", second
    assert executor.submit_clicks == 1
    run = harness.service.runs_for(attempt.id)[0]
    assert "Can you work from the office three days a week?" in run.diagnostics["filled"]
