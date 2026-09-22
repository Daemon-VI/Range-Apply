"""Deterministic executor for tests and benchmarks. No network, ever.

Outcomes are scripted per application id, opportunity id or company name
(first match wins), with a default. ``submit_calls`` counts how many times
submit was actually "pressed" for each application: the duplicate-submission
tests assert on it.
"""

from collections import Counter
from typing import Optional

from app.execution.executors.base import Executor, ExecutorError
from app.execution.models import (
    ErrorClass,
    ExecutionOutcome,
    ExecutionPackage,
    ExecutionResult,
    ExecutionTarget,
    ExecutorKind,
    FieldAnswer,
    FieldType,
    FormField,
    FormOption,
    FormSnapshot,
    HandoffReason,
    VerificationMethod,
    VerificationResult,
    VerificationStatus,
)

SUCCESS = "SUCCESS"
SUCCESS_LIKELY = "SUCCESS_LIKELY"
SUCCESS_UNVERIFIED = "SUCCESS_UNVERIFIED"
RETRYABLE = "RETRYABLE"
PERMANENT = "PERMANENT"
CAPTCHA = "CAPTCHA"
AUTH = "AUTH"
MFA = "MFA"
FORM_CHANGED = "FORM_CHANGED"
UNKNOWN = "UNKNOWN"
UNKNOWN_THEN_VERIFIED = "UNKNOWN_THEN_VERIFIED"
UNKNOWN_THEN_FAILED = "UNKNOWN_THEN_FAILED"
NEEDS_INPUT = "NEEDS_INPUT"
UNKNOWN_FIELD = "UNKNOWN_FIELD"
CRASH_BEFORE_SUBMIT = "CRASH_BEFORE_SUBMIT"
CRASH_AFTER_SUBMIT = "CRASH_AFTER_SUBMIT"

STANDARD_FIELDS: list[FormField] = [
    FormField(external_id="name", label="Full name", field_type=FieldType.TEXT, required=True),
    FormField(external_id="email", label="Email", field_type=FieldType.TEXT, required=True),
    FormField(external_id="resume", label="Resume/CV", field_type=FieldType.FILE, required=True, accept=".pdf,.docx"),
    FormField(external_id="cover", label="Cover letter", field_type=FieldType.FILE, required=False),
    FormField(
        external_id="auth",
        label="Are you legally authorized to work in this country?",
        field_type=FieldType.SELECT,
        required=True,
        options=[FormOption(label="Yes", value="yes"), FormOption(label="No", value="no")],
    ),
    FormField(
        external_id="sponsor",
        label="Will you now or in the future require sponsorship?",
        field_type=FieldType.RADIO,
        required=True,
        options=[FormOption(label="Yes"), FormOption(label="No")],
    ),
    FormField(external_id="why", label="Why are you interested in this role?", field_type=FieldType.TEXTAREA, required=True),
]


class MockExecutor(Executor):
    kind = ExecutorKind.MOCK
    version = "mock-v1"

    def __init__(self, script: Optional[dict[str, str]] = None, default: str = SUCCESS, fields: Optional[list[FormField]] = None):
        self.script = dict(script or {})
        self.default = default
        self.fields = list(fields) if fields is not None else list(STANDARD_FIELDS)
        self.submit_calls: Counter = Counter()
        self.prepare_calls: Counter = Counter()
        self.verify_calls: Counter = Counter()
        self._verified: set[str] = set()

    # ---------------------------------------------------------------- script

    def outcome_for(self, package: ExecutionPackage) -> str:
        for key in (package.application_id, package.opportunity_id, package.target.company):
            if key in self.script:
                return self.script[key]
        return self.default

    # -------------------------------------------------------------- contract

    def can_handle(self, target: ExecutionTarget) -> bool:
        return True

    def prepare(self, package: ExecutionPackage) -> FormSnapshot:
        self.prepare_calls[package.application_id] += 1
        fields = list(self.fields)
        outcome = self.outcome_for(package)
        if outcome == NEEDS_INPUT:
            fields.append(FormField(external_id="salary", label="What are your salary expectations?", field_type=FieldType.NUMERIC, required=True))
        if outcome == UNKNOWN_FIELD:
            fields.append(FormField(external_id="x-widget", label="Rate your alignment with our values", field_type=FieldType.UNKNOWN, required=True))
        return FormSnapshot(source_url=package.target.canonical_url or "mock://form", fields=fields, executor_kind=self.kind, executor_version=self.version, metadata={"scripted": outcome})

    def execute(self, package: ExecutionPackage, form: FormSnapshot, answers: list[FieldAnswer], gate=None) -> ExecutionResult:
        outcome = self.outcome_for(package)
        app_id = package.application_id
        if gate is not None and outcome not in (CRASH_BEFORE_SUBMIT, CAPTCHA, AUTH, MFA, FORM_CHANGED, RETRYABLE, PERMANENT):
            failures = gate()
            if failures:
                return ExecutionResult(outcome=ExecutionOutcome.NEEDS_REVIEW, error_class=ErrorClass.POLICY, message="pre-submit gate failed: " + "; ".join(failures[:3]), stopped_at="before_submit")
        if outcome == CRASH_BEFORE_SUBMIT:
            raise ExecutorError("browser crashed while filling the form", before_submit=True)
        if outcome == CAPTCHA:
            return ExecutionResult(outcome=ExecutionOutcome.HANDOFF, handoff_reason=HandoffReason.CAPTCHA_REQUIRED, stopped_at="captcha", remaining_steps=["solve the CAPTCHA", "press submit"], message="CAPTCHA presented")
        if outcome == AUTH:
            return ExecutionResult(outcome=ExecutionOutcome.HANDOFF, handoff_reason=HandoffReason.AUTH_REQUIRED, stopped_at="login", remaining_steps=["sign in", "press submit"], message="login required")
        if outcome == MFA:
            return ExecutionResult(outcome=ExecutionOutcome.HANDOFF, handoff_reason=HandoffReason.MFA_REQUIRED, stopped_at="mfa", remaining_steps=["complete MFA", "press submit"], message="MFA challenge")
        if outcome == FORM_CHANGED:
            return ExecutionResult(outcome=ExecutionOutcome.FORM_CHANGED, error_class=ErrorClass.FORM_CHANGED, message="form no longer matches the snapshot")
        if outcome == RETRYABLE:
            return ExecutionResult(outcome=ExecutionOutcome.RETRYABLE_FAILURE, error_class=ErrorClass.TRANSIENT_NETWORK, message="connection reset before submit")
        if outcome == PERMANENT:
            return ExecutionResult(outcome=ExecutionOutcome.PERMANENT_FAILURE, error_class=ErrorClass.TARGET_GONE, message="posting returns 404")
        # Everything below presses submit.
        self.submit_calls[app_id] += 1
        if outcome == CRASH_AFTER_SUBMIT:
            raise ExecutorError("browser crashed after pressing submit", before_submit=False)
        if outcome in (UNKNOWN, UNKNOWN_THEN_VERIFIED, UNKNOWN_THEN_FAILED):
            return ExecutionResult(outcome=ExecutionOutcome.UNKNOWN, submit_attempted=True, error_class=ErrorClass.TIMEOUT, message="timeout after pressing submit", diagnostics={"step": "submit", "password": "never-stored"})
        reference = f"mock-{app_id[:8]}-{self.submit_calls[app_id]}"
        return ExecutionResult(
            outcome=ExecutionOutcome.SUBMITTED,
            submit_attempted=True,
            application_url=f"{package.target.canonical_url or 'mock://form'}/confirmation",
            external_application_id=reference,
            confirmation_reference=reference,
            message="confirmation page shown",
            diagnostics={"fields_filled": sum(1 for a in answers if a.status.value == "ANSWERED")},
        )

    def verify(self, package: ExecutionPackage, result: ExecutionResult) -> VerificationResult:
        self.verify_calls[package.application_id] += 1
        outcome = self.outcome_for(package)
        if outcome in (SUCCESS, UNKNOWN_THEN_VERIFIED, NEEDS_INPUT, UNKNOWN_FIELD, CRASH_BEFORE_SUBMIT):
            reference = result.confirmation_reference or f"mock-{package.application_id[:8]}-verified"
            return VerificationResult(status=VerificationStatus.VERIFIED, method=VerificationMethod.CONFIRMATION_PAGE if result.confirmation_reference else VerificationMethod.APPLICATION_HISTORY, detail="confirmation found", confirmation_reference=reference, external_application_id=reference)
        if outcome == SUCCESS_LIKELY:
            return VerificationResult(status=VerificationStatus.LIKELY, method=VerificationMethod.REDIRECT_URL, detail="redirected to a thank-you URL, no reference")
        if outcome == UNKNOWN_THEN_FAILED:
            return VerificationResult(status=VerificationStatus.FAILED, method=VerificationMethod.APPLICATION_HISTORY, detail="no application in the target's history")
        return VerificationResult(status=VerificationStatus.UNKNOWN, method=VerificationMethod.NONE, detail="cannot establish the outcome")
