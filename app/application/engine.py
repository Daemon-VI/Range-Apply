"""Application engine (Phase 5): prepare and submit job applications.

Identity (Blueprint Phase 2/5): an application attempt belongs to
``(tenant, opportunity)``; ``job_id`` is kept for compatibility and always
resolves to that identity. Two source job rows for one opening can never
create two attempts (``uq_applications_tenant_opportunity``).

Safety order in ``submit`` is load-bearing (PRD FR-08 / FR-11): kill-switch,
then idempotency, then explicit approval - each a hard stop before any status
transition or adapter call happens.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.application.adapters.base import ATSAdapter
from app.application.adapters.greenhouse_playwright import GreenhousePlaywrightAdapter
from app.application.database.models import ApplicationEventRow, ApplicationRow
from app.application.killswitch import is_paused
from app.application.models import ApplicationStatus
from app.core.timeutils import db_now
from app.jobs.database.models import JobRow

logger = logging.getLogger(__name__)

#: Attempts in these states have (possibly) reached the employer: they count as
#: applied for cool-down and duplicate purposes. UNCERTAIN is included on
#: purpose: a submit that may have happened is treated as one that did.
SUBMITTED_STATUSES = frozenset(
    {
        ApplicationStatus.SUBMITTING.value,
        ApplicationStatus.SUBMITTED.value,
        ApplicationStatus.VERIFIED.value,
        ApplicationStatus.UNCERTAIN.value,
        ApplicationStatus.INTERVIEWING.value,
    }
)
#: Attempts in these states are in flight toward submission (slot held).
IN_FLIGHT_STATUSES = frozenset(
    {
        ApplicationStatus.QUALIFIED.value,
        ApplicationStatus.SHORTLISTED.value,
        ApplicationStatus.PREPARING.value,
        ApplicationStatus.READY.value,
        ApplicationStatus.AWAITING_APPROVAL.value,
        ApplicationStatus.BLOCKED.value,
        ApplicationStatus.NEEDS_USER_INPUT.value,
        ApplicationStatus.NEEDS_REVIEW.value,
    }
)
TERMINAL_STATUSES = frozenset(
    {
        ApplicationStatus.FAILED.value,
        ApplicationStatus.CANCELLED.value,
        ApplicationStatus.REJECTED.value,
        ApplicationStatus.CLOSED.value,
    }
)


def resolve_opportunity_for_job(db: Session, job_id: str) -> Optional[str]:
    """The shared opportunity a job row belongs to, if it has been resolved."""
    from app.pipeline.database.models import OpportunityJobRow

    link = db.query(OpportunityJobRow).filter_by(job_id=job_id).first()
    return link.opportunity_id if link else None


class ApplicationEngine:
    """Owns the application state machine."""

    def get_or_create(
        self,
        db: Session,
        job_id: str,
        tenant_id: Optional[str] = None,
        opportunity_id: Optional[str] = None,
        preparation_id: Optional[str] = None,
        candidate_opportunity_id: Optional[str] = None,
    ) -> ApplicationRow:
        """One application attempt per opening, carrying its candidate identity.

        Lookup order: ``(tenant_id, opportunity_id)`` when both are known, then
        the legacy ``job_id``. When only a job id is given the opportunity is
        derived from the job's link so the canonical identity is still kept.
        """
        if opportunity_id is None:
            opportunity_id = resolve_opportunity_for_job(db, job_id)
        row = None
        if tenant_id and opportunity_id:
            row = (
                db.query(ApplicationRow)
                .filter_by(tenant_id=tenant_id, opportunity_id=opportunity_id)
                .first()
            )
        if row is None:
            row = db.query(ApplicationRow).filter_by(job_id=job_id).first()
        if row is None:
            row = ApplicationRow(job_id=job_id, status=ApplicationStatus.DISCOVERED.value)
            db.add(row)
        if tenant_id and not row.tenant_id:
            row.tenant_id = tenant_id
        if opportunity_id and not row.opportunity_id:
            row.opportunity_id = opportunity_id
        if candidate_opportunity_id and not row.candidate_opportunity_id:
            row.candidate_opportunity_id = candidate_opportunity_id
        if preparation_id and row.preparation_id != preparation_id:
            row.preparation_id = preparation_id
        db.commit()
        db.refresh(row)
        return row

    def _log_event(
        self,
        db: Session,
        application: ApplicationRow,
        event_type: str,
        from_status: Optional[str],
        to_status: Optional[str],
        metadata: Optional[dict] = None,
    ) -> None:
        db.add(
            ApplicationEventRow(
                application_id=application.id,
                event_type=event_type,
                from_status=from_status,
                to_status=to_status,
                metadata_=metadata or {},
            )
        )

    def prepare(
        self,
        db: Session,
        job_id: str,
        tenant_id: Optional[str] = None,
        opportunity_id: Optional[str] = None,
        preparation_id: Optional[str] = None,
    ) -> ApplicationRow:
        """Move an attempt to READY.

        A READY Phase 4 preparation (``preparation_id``) is the modern input;
        the legacy path still accepts approved ``tailored_artifacts`` for the
        job row so existing callers keep working.
        """
        # Local import: app.tailoring is built concurrently and may not exist
        # on disk at import time of this module.
        from app.tailoring.database.models import TailoredArtifactRow

        application = self.get_or_create(
            db, job_id, tenant_id=tenant_id, opportunity_id=opportunity_id, preparation_id=preparation_id
        )
        artifacts = []
        if preparation_id is None:
            artifacts = (
                db.query(TailoredArtifactRow)
                .filter_by(job_id=job_id, approved=True)
                .all()
            )
            if not artifacts:
                raise ValueError(
                    "No approved tailoring artifacts for this job. Generate and approve "
                    "tailoring artifacts before preparing an application."
                )
        previous_status = application.status

        application.tailored_artifact_ids = [a.id for a in artifacts]
        application.status = ApplicationStatus.PREPARING.value
        db.flush()
        self._log_event(
            db, application, "prepare_started", previous_status, ApplicationStatus.PREPARING.value
        )

        application.status = ApplicationStatus.READY.value
        self._log_event(
            db,
            application,
            "prepared",
            ApplicationStatus.PREPARING.value,
            ApplicationStatus.READY.value,
            metadata={"preparation_id": preparation_id} if preparation_id else None,
        )
        db.commit()
        db.refresh(application)
        return application

    async def submit(
        self,
        db: Session,
        job_id: str,
        approved: bool,
        dry_run: bool = True,
        adapter: Optional[ATSAdapter] = None,
        tenant_id: Optional[str] = None,
        opportunity_id: Optional[str] = None,
    ) -> ApplicationRow:
        """Compatibility wrapper (v5 API). It cannot submit anything.

        Since Blueprint Phase 6/7 every real submission goes through
        ``app.execution.ExecutionService`` (caps, cool-down, blocklist,
        duplicate prevention, preparation validation, idempotency,
        verification). This route keeps the kill-switch and approval checks
        and a *dry run* that records an event without changing the status;
        a live call is refused with a pointer to ``/api/v1/execution``.
        """
        application = self.get_or_create(db, job_id, tenant_id=tenant_id, opportunity_id=opportunity_id)
        job_row = db.query(JobRow).filter_by(id=job_id).first()
        source = job_row.source if job_row else None

        # (1) Kill switch - hard stop, no status change.
        if is_paused(db, source):
            self._log_event(
                db,
                application,
                "blocked_by_killswitch",
                application.status,
                application.status,
                metadata={"source": source},
            )
            db.commit()
            raise RuntimeError("Submission blocked: kill switch is paused.")

        # (2) Idempotency - already submitted, refuse silently and return as-is.
        if application.status in SUBMITTED_STATUSES:
            return application

        # (3) Explicit approval required.
        if approved is not True:
            raise ValueError("Submission requires explicit approval (approved=True).")

        # (4) Legacy boundary: no live submission here, ever.
        if not dry_run:
            self._log_event(
                db, application, "legacy_submit_refused", application.status, application.status,
                metadata={"reason": "live submission moved to /api/v1/execution"},
            )
            db.commit()
            raise RuntimeError(
                "Live submission is not available on the v5 route; enqueue the READY attempt and run the "
                "execution worker (/api/v1/execution) so caps, cool-down, blocklist and idempotency apply."
            )
        if adapter is None:
            # Dry run without an adapter: record it, change nothing, submit nothing.
            self._log_event(db, application, "legacy_dry_run", application.status, application.status, metadata={"dry_run": True})
            application.dry_run = True
            db.commit()
            db.refresh(application)
            return application

        previous_status = application.status
        application.status = ApplicationStatus.AWAITING_APPROVAL.value
        self._log_event(db, application, "approved", previous_status, application.status)

        application.status = ApplicationStatus.SUBMITTING.value
        application.dry_run = dry_run
        self._log_event(
            db,
            application,
            "submitting",
            ApplicationStatus.AWAITING_APPROVAL.value,
            application.status,
        )
        db.commit()

        active_adapter = adapter or GreenhousePlaywrightAdapter()
        application.adapter = getattr(active_adapter, "name", type(active_adapter).__name__)

        try:
            from app.services.career_brain import CareerBrainService

            brain = CareerBrainService(tenant_id=application.tenant_id) if application.tenant_id else CareerBrainService()
            brain.load()
            profile = brain.get_profile()
            prepared = active_adapter.prepare(
                job_row=job_row,
                profile=profile,
                resume_text="",
                cover_letter_text="",
            )
            result = await active_adapter.submit(
                prepared=prepared,
                application_url=(job_row.application_url or job_row.source_url) if job_row else "",
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - adapter failure must never crash the request
            logger.exception("Adapter submission failed for job %s", job_id)
            application.status = ApplicationStatus.FAILED.value
            self._log_event(
                db,
                application,
                "submit_failed",
                ApplicationStatus.SUBMITTING.value,
                application.status,
                metadata={"error": str(exc)},
            )
            db.commit()
            db.refresh(application)
            return application

        if result.success:
            application.status = ApplicationStatus.SUBMITTED.value
            application.confirmation = result.confirmation
            application.submitted_at = db_now()
        else:
            application.status = ApplicationStatus.FAILED.value
            application.confirmation = None

        self._log_event(
            db,
            application,
            "submit_result",
            ApplicationStatus.SUBMITTING.value,
            application.status,
            metadata={"error": result.error, "dry_run": result.dry_run},
        )
        db.commit()
        db.refresh(application)
        return application
