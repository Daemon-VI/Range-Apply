"""Human handoff executor: never submits; hands the package to a person."""

from app.execution.executors.base import Executor
from app.execution.models import (
    ExecutionOutcome,
    ExecutionPackage,
    ExecutionResult,
    ExecutionTarget,
    ExecutorKind,
    FieldAnswer,
    FormSnapshot,
    HandoffReason,
    VerificationMethod,
    VerificationResult,
    VerificationStatus,
)


class ManualExecutor(Executor):
    kind = ExecutorKind.MANUAL
    version = "manual-v1"

    def can_handle(self, target: ExecutionTarget) -> bool:
        return True

    def prepare(self, package: ExecutionPackage) -> FormSnapshot:
        return FormSnapshot(source_url=package.target.canonical_url, fields=[], executor_kind=self.kind, executor_version=self.version)

    def execute(self, package: ExecutionPackage, form: FormSnapshot, answers: list[FieldAnswer], gate=None) -> ExecutionResult:
        return ExecutionResult(
            outcome=ExecutionOutcome.HANDOFF,
            submit_attempted=False,
            handoff_reason=HandoffReason.USER_CONFIRMATION_REQUIRED,
            message="Apply in your browser with the prepared package, then confirm the outcome.",
            stopped_at="before_form",
            remaining_steps=["open the application form", "fill it from the package", "submit", "record the confirmation"],
            resumable=True,
        )

    def verify(self, package: ExecutionPackage, result: ExecutionResult) -> VerificationResult:
        return VerificationResult(status=VerificationStatus.UNKNOWN, method=VerificationMethod.USER_CONFIRMATION, detail="waiting for the candidate to confirm")
