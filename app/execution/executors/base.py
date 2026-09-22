"""The executor contract.

An executor receives an :class:`ExecutionPackage` (prepared material, never
raw candidate data) and reports an :class:`ExecutionResult`. It must not
persist anything itself; the :class:`ExecutionService` owns every state
transition. Verification is a separate call so a click is never mistaken for
a submission.

    can_handle(target)  -> may this executor apply to this target?
    prepare(package)    -> discover the form (a FormSnapshot); no submission
    execute(package, form, answers) -> fill and submit; report honestly,
                                       including UNKNOWN when unsure
    verify(package, result)         -> establish what actually happened

Raise :class:`ExecutorError` for crashes. ``before_submit=True`` tells the
service that submit was certainly not pressed (safe to retry); anything else
is treated as UNKNOWN and routed to verification, never to a resubmit.
"""

from typing import Protocol, runtime_checkable

from app.execution.models import (
    ErrorClass,
    ExecutionPackage,
    ExecutionResult,
    ExecutionTarget,
    ExecutorKind,
    FieldAnswer,
    FormSnapshot,
    VerificationResult,
)


class ExecutorError(Exception):
    def __init__(self, message: str, *, before_submit: bool, retryable: bool = True, error_class: ErrorClass = ErrorClass.EXECUTOR_CRASH):
        super().__init__(message)
        self.message = message
        self.before_submit = before_submit
        self.retryable = retryable
        self.error_class = error_class


@runtime_checkable
class Executor(Protocol):
    kind: ExecutorKind
    version: str

    def can_handle(self, target: ExecutionTarget) -> bool: ...

    def prepare(self, package: ExecutionPackage) -> FormSnapshot: ...

    def execute(self, package: ExecutionPackage, form: FormSnapshot, answers: list[FieldAnswer], gate=None) -> ExecutionResult:
        """``gate`` (optional) re-runs the submission-time preconditions; call it
        right before pressing submit and stop if it returns any failure."""
        ...

    def verify(self, package: ExecutionPackage, result: ExecutionResult) -> VerificationResult: ...
