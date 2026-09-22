"""A dry-run executor can never be recorded as having possibly pressed submit (2026-09-14, Okta).

The real dry run hit a CAPTCHA after ``execute`` had already recorded
``submit_invoked`` — the run kept a false "submit may have been pressed" mark.
"""

from app.execution.executors.mock import MockExecutor
from app.execution.models import ExecutionOutcome, ExecutionResult, ExecutorKind, HandoffReason


class _DryRunCaptcha(MockExecutor):
    dry_run = True

    def execute(self, package, form, answers, gate=None):
        return ExecutionResult(outcome=ExecutionOutcome.HANDOFF, handoff_reason=HandoffReason.CAPTCHA_REQUIRED, stopped_at="captcha", message="CAPTCHA presented")


class _DryRunCrash(MockExecutor):
    dry_run = True

    def execute(self, package, form, answers, gate=None):
        raise RuntimeError("browser went away")


class _LiveCrash(MockExecutor):
    dry_run = False

    def execute(self, package, form, answers, gate=None):
        raise RuntimeError("browser went away")


def _run(harness, executor, company):
    harness.service.executors[ExecutorKind.MOCK] = executor
    attempt = harness.ready(company=company)
    outcome = harness.execute(attempt)
    run = harness.service.require_run(outcome["run_id"])
    return outcome, run, attempt


def test_a_dry_run_that_hands_off_never_marks_submit(harness):
    outcome, run, attempt = _run(harness, _DryRunCaptcha(), "Dry Captcha Co")
    assert outcome["outcome"] == "HANDOFF" and run.submit_invoked is False
    assert attempt.status != "UNCERTAIN"


def test_a_dry_run_crash_is_retryable_not_unknown(harness):
    outcome, run, attempt = _run(harness, _DryRunCrash(), "Dry Crash Co")
    assert outcome["outcome"] == "RETRYABLE_FAILURE" and run.submit_invoked is False
    assert attempt.status != "UNCERTAIN"


def test_a_live_crash_is_still_unknown_and_never_resubmitted(harness):
    outcome, run, attempt = _run(harness, _LiveCrash(), "Live Crash Co")
    assert outcome["outcome"] == "UNKNOWN" and run.submit_invoked is True and attempt.status == "UNCERTAIN"
