"""Application engine (Phase 5): prepare and submit job applications.

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


class ApplicationEngine:
    """Owns the application state machine."""

    def get_or_create(self, db: Session, job_id: str) -> ApplicationRow:
        row = db.query(ApplicationRow).filter_by(job_id=job_id).first()
        if row is not None:
            return row
        row = ApplicationRow(job_id=job_id, status=ApplicationStatus.DISCOVERED.value)
        db.add(row)
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

    def prepare(self, db: Session, job_id: str) -> ApplicationRow:
        # Local import: app.tailoring is built concurrently and may not exist
        # on disk at import time of this module.
        from app.tailoring.database.models import TailoredArtifactRow

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

        application = self.get_or_create(db, job_id)
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
    ) -> ApplicationRow:
        application = self.get_or_create(db, job_id)
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
        if application.status == ApplicationStatus.SUBMITTED.value:
            return application

        # (3) Explicit approval required.
        if approved is not True:
            raise ValueError("Submission requires explicit approval (approved=True).")

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

            brain = CareerBrainService()
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
