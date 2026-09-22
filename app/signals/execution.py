"""Execution results → the Signal Inbox (Blueprint Phase 10 §5).

Phase 6/7/9 already decide what page evidence is worth (``Executor.verify``
→ VERIFIED / LIKELY / UNKNOWN / FAILED). This module does not re-verify; it
records that decision as a signal so it is attributed, turned into an
outcome event and traceable next to employer signals. The reference is the
run plus its verification state, so re-reporting the same result is an
observation, and an upgrade (UNKNOWN → VERIFIED after confirmation) is a new
signal with its own evidence.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.application.database.models import ApplicationRow
from app.execution.database.models import ExecutionRunRow
from app.execution.models import ExecutorKind, VerificationStatus
from app.signals.models import AttributionHints, SignalIngest, SignalSource

logger = logging.getLogger(__name__)

_SOURCE_FOR_EXECUTOR = {ExecutorKind.PLAYWRIGHT_LOCAL.value: SignalSource.PLAYWRIGHT, ExecutorKind.BROWSER_EXTENSION.value: SignalSource.EXTENSION}


def execution_signal(run: ExecutionRunRow, attempt: ApplicationRow, actor: str = "execution") -> SignalIngest:
    """The ingestion request for one run's current result (pure)."""
    verification = run.verification_status or VerificationStatus.NOT_ATTEMPTED.value
    method = run.verification_method or "-"
    reference = f"execution_run:{run.id}:{verification}:{method}"
    detail = (run.verification_detail or run.error_message or "")[:300]
    text = f"Execution run {run.run_number} ({run.executor_kind}) finished {run.status}; verification {verification} via {method}. {detail}".strip()
    payload = {
        "kind": "execution_result",
        "run_id": run.id,
        "run_number": run.run_number,
        "executor_kind": run.executor_kind,
        "executor_version": run.executor_version,
        "run_status": run.status,
        "run_outcome": run.outcome,
        "submit_invoked": bool(run.submit_invoked),
        "verification_status": verification,
        "verification_method": run.verification_method,
        "verification_detail": detail or None,
        "external_application_id": run.external_application_id,
        "confirmation_reference": run.confirmation_reference,
        "application_url": (run.application_url or "")[:300] or None,
    }
    return SignalIngest(
        source=_SOURCE_FOR_EXECUTOR.get(run.executor_kind, SignalSource.EXECUTION),
        source_reference=reference,
        subject=f"Execution result: {run.status} / verification {verification}",
        text=text,
        external_at=run.finished_at,
        payload=payload,
        provenance={"connector": "execution_service", "actor": actor, "worker_id": run.worker_id},
        hints=AttributionHints(application_id=attempt.id, execution_run_id=run.id, opportunity_id=attempt.opportunity_id, candidate_opportunity_id=attempt.candidate_opportunity_id, reference=run.confirmation_reference or run.external_application_id),
    )


def emit_execution_signal(db: Session, tenant_id: str, run: ExecutionRunRow, attempt: ApplicationRow, actor: str = "execution") -> Optional[tuple]:
    """Ingest the run's result as a signal. Returns ``(signal, created)``."""
    from app.signals.service import SignalIngestionService

    service = SignalIngestionService(db, tenant_id, actor=actor)
    return service.ingest(execution_signal(run, attempt, actor))


__all__ = ["emit_execution_signal", "execution_signal"]
