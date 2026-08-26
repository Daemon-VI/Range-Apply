"""Deduplication and identity resolution layer for job ingestion."""

import logging
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse, urlunparse

from sqlalchemy.orm import Session

from app.jobs.database.models import JobRow, JobVersionRow, SourceReferenceRow
from app.jobs.models.job import NormalizedJob

logger = logging.getLogger(__name__)


def clean_url(url: Optional[str]) -> Optional[str]:
    """Strips query tracking parameters and fragments from a URL for canonical matching."""
    if not url:
        return None
    try:
        parsed = urlparse(url.strip())
        cleaned = urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))
        return cleaned
    except Exception:
        return url.strip()


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
    ):
        self.status = status
        self.job_row = job_row
        self.is_content_changed = is_content_changed
        self.changes_summary = changes_summary


class JobDeduplicator:
    """Resolves job identity using a 5-level deterministic hierarchy.
    
    Hierarchy:
    1. Exact (source, source_job_id) in jobs or source_references
    2. Canonical application URL match
    3. Canonical source URL match
    4. Canonical key (company + normalized_title + location)
    5. Content hash match (for same company and location)
    """

    def find_existing_job(self, db: Session, job: NormalizedJob) -> Optional[JobRow]:
        """Finds matching existing canonical JobRow using deterministic hierarchy."""
        source_str = job.source.value
        source_job_id_str = job.source_job_id

        # Level 1a: Exact match on jobs table (source, source_job_id)
        existing = db.query(JobRow).filter_by(source=source_str, source_job_id=source_job_id_str).first()
        if existing:
            return existing

        # Level 1b: Exact match on job_source_references table
        ref = db.query(SourceReferenceRow).filter_by(source=source_str, source_job_id=source_job_id_str).first()
        if ref and ref.job:
            return ref.job

        # Level 2: Canonical application URL
        clean_apply = clean_url(job.application_url)
        if clean_apply:
            match = db.query(JobRow).filter(JobRow.application_url.isnot(None)).filter(JobRow.application_url.like(f"{clean_apply}%")).first()
            if match:
                return match

        # Level 3: Canonical source URL
        clean_src = clean_url(job.source_url)
        if clean_src:
            match = db.query(JobRow).filter(JobRow.source_url.like(f"{clean_src}%")).first()
            if match:
                return match

        # Level 4: Canonical key (company + title + location)
        match_key = db.query(JobRow).filter_by(canonical_key=job.canonical_key).first()
        if match_key:
            return match_key

        # Level 5: Exact content hash match for the same company and location
        if job.content_hash:
            query = db.query(JobRow).filter_by(company=job.company, content_hash=job.content_hash)
            if job.location:
                query = query.filter_by(location=job.location)
            match_hash = query.first()
            if match_hash:
                return match_hash

        return None

    def process(self, db: Session, job: NormalizedJob) -> DeduplicationResult:
        """Processes a normalized job against the database, handling insert, update, and provenance."""
        existing = self.find_existing_job(db, job)
        now = datetime.utcnow()
        source_str = job.source.value
        source_job_id_str = job.source_job_id

        if existing is None:
            # Level 0: Completely new job
            new_row = JobRow(
                canonical_key=job.canonical_key,
                source=source_str,
                source_job_id=source_job_id_str,
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
                posted_at=job.posted_at,
                deadline=job.deadline,
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

            # Record initial source reference
            ref = SourceReferenceRow(
                job_id=new_row.id,
                source=source_str,
                source_job_id=source_job_id_str,
                source_url=job.source_url,
                application_url=job.application_url,
                first_seen_at=now,
                last_seen_at=now,
                metadata_=job.metadata,
            )
            db.add(ref)
            db.commit()

            logger.info("New job persisted: '%s' at %s (id: %s)", new_row.title, new_row.company, new_row.id)
            return DeduplicationResult(status="NEW", job_row=new_row)

        # Existing job found: check if this is a cross-source appearance or content change
        existing.last_seen_at = now

        # Ensure SourceReference exists for this source + job_id
        ref = db.query(SourceReferenceRow).filter_by(source=source_str, source_job_id=source_job_id_str).first()
        is_cross_source = False
        if ref is None:
            is_cross_source = True
            ref = SourceReferenceRow(
                job_id=existing.id,
                source=source_str,
                source_job_id=source_job_id_str,
                source_url=job.source_url,
                application_url=job.application_url,
                first_seen_at=now,
                last_seen_at=now,
                metadata_=job.metadata,
            )
            db.add(ref)
        else:
            ref.last_seen_at = now

        # Check if content has meaningfully changed
        if existing.content_hash != job.content_hash:
            # Create a version record
            changes = f"Content updated from hash {existing.content_hash[:8]} to {job.content_hash[:8]}"
            version = JobVersionRow(
                job_id=existing.id,
                content_hash=existing.content_hash,
                title=existing.title,
                description=existing.description,
                changes_summary=changes,
                created_at=now,
            )
            db.add(version)

            # Update current fields
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
            existing.technologies = job.technologies
            existing.responsibilities = job.responsibilities
            existing.qualifications = job.qualifications
            existing.salary_text = job.salary_text
            existing.content_hash = job.content_hash
            existing.updated_at = now

            db.commit()
            logger.info("Job updated with version: '%s' at %s (id: %s)", existing.title, existing.company, existing.id)
            return DeduplicationResult(
                status="UPDATED",
                job_row=existing,
                is_content_changed=True,
                changes_summary=changes,
            )

        db.commit()
        status = "CROSS_SOURCE_DUPLICATE" if is_cross_source else "DUPLICATE"
        return DeduplicationResult(status=status, job_row=existing)
