"""The browser-extension executor (Blueprint Phase 9).

The extension runs in the candidate's own browser and drives the same API
flow an external executor was designed for in Phase 6:

    match (tab URL -> claimable SUBMIT item) -> claim -> start
    -> form (discovered fields; the server maps answers, one mapper)
    -> fill (rendered documents fetched from /api/v1/documents/{id}/file)
    -> gate (server re-runs every precondition and marks submit_invoked)
    -> the person or the extension presses submit
    -> result (evidence) -> verify | handoff (CAPTCHA / login / MFA / ...)

This class is the in-process face of that executor: it owns ``can_handle``
and the conservative ``verify`` applied to what the extension reports. It
cannot ``execute`` in-process on purpose: the browser is the person's.
"""

import re

from app.core.errors import ValidationFailed
from app.execution.executors.base import Executor
from app.execution.models import (
    ExecutionOutcome,
    ExecutionPackage,
    ExecutionResult,
    ExecutionTarget,
    ExecutorKind,
    FieldAnswer,
    FormSnapshot,
    VerificationMethod,
    VerificationResult,
    VerificationStatus,
)

_CONFIRMATION_URL = re.compile(r"confirmation|thank|thanks|submitted|success|applied", re.IGNORECASE)


class BrowserExtensionExecutor(Executor):
    kind = ExecutorKind.BROWSER_EXTENSION
    version = "extension-v1"

    def can_handle(self, target: ExecutionTarget) -> bool:
        url = (target.canonical_url or "").lower()
        return target.method.value == "browser_form" and (url.startswith("http://") or url.startswith("https://") or url.startswith("file://"))

    def prepare(self, package: ExecutionPackage) -> FormSnapshot:
        raise ValidationFailed("BROWSER_EXTENSION executes in the candidate's browser; use the /api/v1/execution/extension flow")

    def execute(self, package: ExecutionPackage, form: FormSnapshot, answers: list[FieldAnswer], gate=None) -> ExecutionResult:
        raise ValidationFailed("BROWSER_EXTENSION executes in the candidate's browser; use the /api/v1/execution/extension flow")

    def verify(self, package: ExecutionPackage, result: ExecutionResult) -> VerificationResult:
        """Only evidence the extension observed on the page counts; a click never does."""
        if result.outcome is not ExecutionOutcome.SUBMITTED:
            return VerificationResult(status=VerificationStatus.UNKNOWN, method=VerificationMethod.NONE, detail="no submission reported")
        reference = result.confirmation_reference or result.external_application_id
        if reference:
            return VerificationResult(status=VerificationStatus.VERIFIED, method=VerificationMethod.APPLICATION_ID, detail=f"reference reported from the confirmation page: {reference}", confirmation_reference=reference, external_application_id=reference, application_url=result.application_url)
        marker = (result.diagnostics or {}).get("success_marker")
        if marker:
            return VerificationResult(status=VerificationStatus.VERIFIED, method=VerificationMethod.CONFIRMATION_TEXT, detail=f"confirmation text observed: {str(marker)[:80]}", application_url=result.application_url)
        url = result.application_url or ""
        if url and url != package.target.canonical_url and _CONFIRMATION_URL.search(url):
            return VerificationResult(status=VerificationStatus.LIKELY, method=VerificationMethod.REDIRECT_URL, detail="redirected to a confirmation-looking URL without confirmation text", application_url=url)
        return VerificationResult(status=VerificationStatus.UNKNOWN, method=VerificationMethod.NONE, detail="the extension reported a submit but no confirmation evidence")
