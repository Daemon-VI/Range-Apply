"""Adapter from the persisted ``JobRow`` to the domain ``NormalizedJob``.

This is the piece Phase 3 was missing. The intelligence services are written
against the Pydantic domain model, while discovery stores SQLAlchemy rows, and
nothing bridged the two — which is why the match engine could only ever run in
tests against hand-built fixtures.

It is a pure field mapping: **no parsing or re-extraction happens here.**
Whatever Phase 2 normalized is what Phase 3 reasons about, so a job's
requirements can never differ between the two layers.
"""

from typing import Optional

from app.core.timeutils import from_db
from app.jobs.database.models import JobRow
from app.jobs.models.enums import (
    EmploymentType,
    ExperienceLevel,
    JobSourceType,
    JobStatus,
    ProcessingStatus,
    RemoteType,
)
from app.jobs.models.job import GraduationRequirement, NormalizedJob


def _enum(enum_cls, value, default):
    """Coerce a stored string into an enum, falling back to ``default``."""
    if value is None:
        return default
    try:
        return enum_cls(value)
    except ValueError:
        return default


def _graduation_requirement(raw) -> Optional[GraduationRequirement]:
    """Rebuild the structured graduation requirement from its JSON column."""
    if not raw or not isinstance(raw, dict):
        return None
    # An all-empty dict is how "no requirement" was persisted.
    if not any(
        raw.get(key) for key in ("minimum_year", "maximum_year", "exact_years", "original_text")
    ):
        return None
    try:
        return GraduationRequirement(**raw)
    except (TypeError, ValueError):
        return None


def job_row_to_normalized(row: JobRow) -> NormalizedJob:
    """Convert a persisted job into the canonical domain model."""
    return NormalizedJob(
        id=row.id,
        canonical_key=row.canonical_key,
        source=_enum(JobSourceType, row.source, JobSourceType.OTHER),
        source_job_id=row.source_job_id,
        source_identifier=row.source_identifier,
        company=row.company,
        title=row.title,
        original_title=row.original_title or row.title,
        description=row.description or "",
        original_description=row.original_description or "",
        location=row.location,
        locations=list(row.locations or []),
        remote_type=_enum(RemoteType, row.remote_type, RemoteType.UNKNOWN),
        employment_type=_enum(EmploymentType, row.employment_type, EmploymentType.UNKNOWN),
        experience_level=_enum(ExperienceLevel, row.experience_level, ExperienceLevel.UNKNOWN),
        education_requirements=list(row.education_requirements or []),
        graduation_requirement=_graduation_requirement(row.graduation_requirement),
        graduation_year_requirement=row.graduation_year_requirement,
        required_skills=list(row.required_skills or []),
        preferred_skills=list(row.preferred_skills or []),
        technologies=list(row.technologies or []),
        responsibilities=list(row.responsibilities or []),
        qualifications=list(row.qualifications or []),
        salary_text=row.salary_text,
        application_url=row.application_url,
        source_url=row.source_url,
        posted_at=from_db(row.posted_at),
        source_updated_at=from_db(row.source_updated_at),
        deadline=from_db(row.deadline),
        first_seen_at=from_db(row.first_seen_at),
        last_seen_at=from_db(row.last_seen_at),
        content_hash=row.content_hash or "",
        processing_status=_enum(ProcessingStatus, row.processing_status, ProcessingStatus.NORMALIZED),
        job_status=_enum(JobStatus, row.job_status, JobStatus.UNKNOWN),
        extraction_metadata=dict(row.extraction_metadata or {}),
        metadata=dict(row.metadata_ or {}),
    )
