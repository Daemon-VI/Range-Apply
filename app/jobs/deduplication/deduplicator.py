"""Deduplication and identity resolution layer for job ingestion.

Identity is resolved by a deterministic hierarchy, strongest signal first. Every
level compares **exact values**: no prefix matching, no fuzzy merging. When no
level matches, the job is inserted as new — preserving a possible duplicate is
always safer than silently collapsing two distinct postings.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.core.timeutils import db_now, to_db
from app.jobs.database.models import JobRow, JobVersionRow, SourceReferenceRow
from app.jobs.models.job import NormalizedJob
from app.jobs.normalization.urls import normalize_url

logger = logging.getLogger(__name__)

STATUS_NEW = "NEW"
STATUS_UPDATED = "UPDATED"
STATUS_DUPLICATE = "DUPLICATE"
STATUS_CROSS_SOURCE_DUPLICATE = "CROSS_SOURCE_DUPLICATE"


def clean_url(url: Optional[str]) -> Optional[str]:
    """Deprecated alias for :func:`app.jobs.normalization.urls.normalize_url`.

    Kept so existing imports keep working; new code should import
    ``normalize_url`` directly.
    """
    return normalize_url(url)


def dump_model(model) -> dict:
    """Helper to dump pydantic model cleanly in both v1 and v2."""
    if model is None:
        return {}
    if hasattr(model, "model_dump"):
        return model.model_dump()
    if hasattr(model, "dict"):
        return model.dict()
    return dict(model)


class DeduplicationResult:
    """Outcome of deduplication resolution for a normalized job."""

    def __init__(
        self,
        status: str,  # "NEW" | "UPDATED" | "DUPLICATE" | "CROSS_SOURCE_DUPLICATE"
        job_row: JobRow,
        is_content_changed: bool = False,
        changes_summary: Optional[str] = None,
        matched_by: Optional[str] = None,
    ):
        self.status = status
        self.job_row = job_row
        self.is_content_changed = is_content_changed
        self.changes_summary = changes_summary
        # Which identity level resolved this job — recorded for auditability.
        self.matched_by = matched_by


class JobDeduplicator:
    """Resolves job identity using a 5-level deterministic hierarchy.

    Hierarchy (first match wins):

    1. Exact ``(source, source_job_id)`` in ``jobs`` or ``job_source_references``
    2. Exact normalized application URL
    3. Exact normalized source URL
    4. Canonical key (company + normalized title + location)
    5. Content hash, scoped to the same company and location

    Levels 2 and 3 previously used ``LIKE '<url>%'``, which merged any posting
    whose URL was a prefix of another (``/jobs/123`` swallowing ``/jobs/1234``).
    They now compare the canonical form produced by ``normalize_url`` with
    ``==``, so a prefix collision is structurally impossible.
    """

    def find_existing_job(self, db: Session, job: NormalizedJob) -> tuple:
        """Find the matching canonical row, if any.

        Returns:
            ``(JobRow | None, matched_by: str | None)``
        """
        source_str = job.source.value
        source_job_id_str = job.source_job_id

        # Level 1a: exact match on jobs table (source, source_job_id)
        existing = (
            db.query(JobRow).filter_by(source=source_str, source_job_id=source_job_id_str).first()
        )
        if existing:
            return existing, "source_job_id"

        # Level 1b: exact match on job_source_references table
        ref = (
            db.query(SourceReferenceRow)
            .filter_by(source=source_str, source_job_id=source_job_id_str)
            .first()
        )
        if ref and ref.job:
            return ref.job, "source_reference"

        # Level 2: exact normalized application URL
        normalized_apply = normalize_url(job.application_url)
        if normalized_apply:
            match = (
                db.query(JobRow)
                .filter(JobRow.normalized_application_url == normalized_apply)
                .first()
            )
            if match:
                return match, "application_url"

        # Level 3: exact normalized source URL
        normalized_source = normalize_url(job.source_url)
        if normalized_source:
            match = db.query(JobRow).filter(JobRow.normalized_source_url == normalized_source).first()
            if match:
                return match, "source_url"

        # Level 4: canonical key (company + title + location)
        match_key = db.query(JobRow).filter_by(canonical_key=job.canonical_key).first()
        if match_key:
            return match_key, "canonical_key"

        # Level 5: identical content for the same company and location
        if job.content_hash:
            query = db.query(JobRow).filter_by(company=job.company, content_hash=job.content_hash)
            if job.location:
                query = query.filter_by(location=job.location)
            match_hash = query.first()
            if match_hash:
                return match_hash, "content_hash"

        return None, None

    def process(self, db: Session, job: NormalizedJob, commit: bool = True) -> DeduplicationResult:
        """Resolve a normalized job against the database.

        Args:
            db: Active session.
            job: The normalized job to insert, update or link.
            commit: Whether to commit. The ingestion pipeline passes ``False``
                and owns the transaction boundary itself, so a run is not split
                into one transaction per job.
        """
        existing, matched_by = self.find_existing_job(db, job)
        now = db_now()
        source_str = job.source.value
        source_job_id_str = job.source_job_id
        normalized_source = normalize_url(job.source_url)
        normalized_apply = normalize_url(job.application_url)

        if existing is None:
            new_row = JobRow(
                canonical_key=job.canonical_key,
                source=source_str,
                source_job_id=source_job_id_str,
                source_identifier=job.source_identifier,
                company=job.company,
                title=job.title,
                original_title=job.original_title,
                description=job.description,
                original_description=job.original_description,
                location=job.location,
                locations=job.locations,
                remote_type=job.remote_type.value,
                employment_type=job.employment_type.value,
                experience_level=job.experience_level.value,
                education_requirements=job.education_requirements,
                graduation_requirement=dump_model(job.graduation_requirement),
                graduation_year_requirement=job.graduation_year_requirement,
                required_skills=job.required_skills,
                preferred_skills=job.preferred_skills,
                technologies=job.technologies,
                responsibilities=job.responsibilities,
                qualifications=job.qualifications,
                salary_text=job.salary_text,
                application_url=job.application_url,
                source_url=job.source_url,
                normalized_source_url=normalized_source,
                normalized_application_url=normalized_apply,
                posted_at=to_db(job.posted_at),
                source_updated_at=to_db(job.source_updated_at),
                deadline=to_db(job.deadline),
                first_seen_at=now,
                last_seen_at=now,
                content_hash=job.content_hash,
                processing_status=job.processing_status.value,
                job_status=job.job_status.value,
                extraction_metadata=job.extraction_metadata,
                metadata_=job.metadata,
            )
            db.add(new_row)
            db.flush()

            db.add(
                SourceReferenceRow(
                    job_id=new_row.id,
                    source=source_str,
                    source_job_id=source_job_id_str,
                    source_url=job.source_url,
                    application_url=job.application_url,
                    first_seen_at=now,
                    last_seen_at=now,
                    metadata_=job.metadata,
                )
            )
            if commit:
                db.commit()
            else:
                db.flush()

            logger.info(
                "New job persisted: '%s' at %s (id: %s)", new_row.title, new_row.company, new_row.id
            )
            return DeduplicationResult(status=STATUS_NEW, job_row=new_row, matched_by=None)

        # Existing job: record this sighting, then decide whether content moved.
        existing.last_seen_at = now
        # A job seen again on its source is live again, whatever a previous
        # sweep concluded.
        if existing.job_status in ("CLOSED", "EXPIRED"):
            existing.job_status = job.job_status.value
            existing.closed_at = None
        if not existing.normalized_source_url and normalized_source:
            existing.normalized_source_url = normalized_source
        if not existing.normalized_application_url and normalized_apply:
            existing.normalized_application_url = normalized_apply
        if not existing.source_identifier and job.source_identifier:
            existing.source_identifier = job.source_identifier
        if job.posted_at and not existing.posted_at:
            existing.posted_at = to_db(job.posted_at)
        if job.source_updated_at:
            existing.source_updated_at = to_db(job.source_updated_at)

        ref = (
            db.query(SourceReferenceRow)
            .filter_by(source=source_str, source_job_id=source_job_id_str)
            .first()
        )
        is_cross_source = False
        if ref is None:
            is_cross_source = True
            db.add(
                SourceReferenceRow(
                    job_id=existing.id,
                    source=source_str,
                    source_job_id=source_job_id_str,
                    source_url=job.source_url,
                    application_url=job.application_url,
                    first_seen_at=now,
                    last_seen_at=now,
                    metadata_=job.metadata,
                )
            )
        else:
            ref.last_seen_at = now

        if existing.content_hash != job.content_hash:
            changes = (
                f"Content updated from hash {existing.content_hash[:8]} to {job.content_hash[:8]}"
            )
            db.add(
                JobVersionRow(
                    job_id=existing.id,
                    content_hash=existing.content_hash,
                    title=existing.title,
                    description=existing.description,
                    changes_summary=changes,
                    created_at=now,
                )
            )

            existing.title = job.title
            existing.description = job.description
            existing.original_description = job.original_description
            existing.location = job.location
            existing.locations = job.locations
            existing.remote_type = job.remote_type.value
            existing.employment_type = job.employment_type.value
            existing.experience_level = job.experience_level.value
            existing.education_requirements = job.education_requirements
            existing.graduation_requirement = dump_model(job.graduation_requirement)
            existing.graduation_year_requirement = job.graduation_year_requirement
            existing.required_skills = job.required_skills
            existing.preferred_skills = job.preferred_skills
            existing.technologies = job.technologies
            existing.responsibilities = job.responsibilities
            existing.qualifications = job.qualifications
            existing.salary_text = job.salary_text
            existing.content_hash = job.content_hash
            existing.updated_at = now

            if commit:
                db.commit()
            else:
                db.flush()
            logger.info(
                "Job updated with version: '%s' at %s (id: %s)",
                existing.title,
                existing.company,
                existing.id,
            )
            return DeduplicationResult(
                status=STATUS_UPDATED,
                job_row=existing,
                is_content_changed=True,
                changes_summary=changes,
                matched_by=matched_by,
            )

        if commit:
            db.commit()
        else:
            db.flush()
        status = STATUS_CROSS_SOURCE_DUPLICATE if is_cross_source else STATUS_DUPLICATE
        return DeduplicationResult(status=status, job_row=existing, matched_by=matched_by)
