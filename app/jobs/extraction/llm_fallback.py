"""LLM fallback integration: merges semantic LLM output into NormalizedJob without overriding deterministic truth."""

import logging
from typing import Optional

from app.jobs.extraction.llm_provider import LLMProvider, get_llm_provider
from app.jobs.models.enums import EmploymentType, ExperienceLevel, RemoteType
from app.jobs.models.job import GraduationRequirement, NormalizedJob
from app.jobs.models.raw_job import RawJob

logger = logging.getLogger(__name__)


class LLMFallbackExtractor:
    """Enhances a NormalizedJob using LLM semantic parsing for unresolved fields."""

    def __init__(self, provider: Optional[LLMProvider] = None):
        self.provider = provider or get_llm_provider()

    async def enrich_if_needed(self, job: NormalizedJob, raw_job: RawJob) -> NormalizedJob:
        """Invokes LLM only if ambiguous fields remain unresolved deterministically."""
        needs_enrichment = (
            job.employment_type == EmploymentType.UNKNOWN
            or job.remote_type == RemoteType.UNKNOWN
            or job.experience_level == ExperienceLevel.UNKNOWN
            or (job.graduation_requirement is None and "graduat" in raw_job.raw_content.lower())
        )

        if not needs_enrichment:
            return job

        try:
            extractions = await self.provider.extract_ambiguous_fields(job.title, raw_job.raw_content)
            if not extractions:
                return job

            # Only fill fields that were previously UNKNOWN
            if job.employment_type == EmploymentType.UNKNOWN and extractions.get("employment_type"):
                try:
                    job.employment_type = EmploymentType(extractions["employment_type"])
                except ValueError:
                    pass

            if job.remote_type == RemoteType.UNKNOWN and extractions.get("remote_type"):
                try:
                    job.remote_type = RemoteType(extractions["remote_type"])
                except ValueError:
                    pass

            if job.experience_level == ExperienceLevel.UNKNOWN and extractions.get("experience_level"):
                try:
                    job.experience_level = ExperienceLevel(extractions["experience_level"])
                except ValueError:
                    pass

            if job.graduation_requirement is None and (extractions.get("graduation_minimum_year") or extractions.get("graduation_maximum_year")):
                min_yr = extractions.get("graduation_minimum_year")
                max_yr = extractions.get("graduation_maximum_year")
                job.graduation_requirement = GraduationRequirement(
                    minimum_year=min_yr,
                    maximum_year=max_yr,
                    exact_years=[min_yr] if min_yr and min_yr == max_yr else [],
                    original_text="Extracted via LLM fallback",
                    extraction_confidence=0.85,
                )

            if not job.salary_text and extractions.get("salary_text"):
                job.salary_text = extractions["salary_text"]

            job.extraction_metadata["llm_fallback_used"] = True

        except Exception as e:
            logger.warning("LLM fallback enrichment error for job '%s': %s", job.title, e)

        return job
