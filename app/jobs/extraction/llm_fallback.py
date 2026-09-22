"""LLM fallback integration: merges semantic LLM output into NormalizedJob without overriding deterministic truth.

AI is optional here by construction: with the default ``StubProvider`` no
model is called at all, and the extractor reports zero ``calls``. When a
real provider is configured it is consulted only for fields deterministic
extraction left UNKNOWN, and every call/cache hit is counted so the
discovery run can record it.
"""

import logging
from typing import Optional

from app.jobs.extraction.llm_provider import (
    LLMProvider,
    StubProvider,
    get_llm_provider,
)
from app.jobs.models.enums import EmploymentType, ExperienceLevel, RemoteType
from app.jobs.models.job import GraduationRequirement, NormalizedJob
from app.jobs.models.raw_job import RawJob

logger = logging.getLogger(__name__)


class LLMFallbackExtractor:
    """Enhances a NormalizedJob using LLM semantic parsing for unresolved fields."""

    def __init__(self, provider: Optional[LLMProvider] = None):
        self.provider = provider or get_llm_provider()
        #: Provider invocations that could cost money (stub calls are not counted).
        self.calls = 0
        #: Times the on-disk cache answered instead of the provider.
        self.cache_hits = 0
        #: Jobs that needed enrichment but had no real provider configured.
        self.skipped_no_provider = 0

    @property
    def enabled(self) -> bool:
        return not isinstance(self.provider, StubProvider)

    def start_run(self) -> None:
        """A fresh per-run call budget (Phase 8b): the gateway refuses calls
        beyond ``AI_MAX_CALLS_PER_RUN`` within one discovery run."""
        reset = getattr(self.provider, "new_budget", None)
        if callable(reset):
            reset()

    @staticmethod
    def needs_enrichment(job: NormalizedJob, raw_job: RawJob) -> bool:
        return (
            job.employment_type == EmploymentType.UNKNOWN
            or job.remote_type == RemoteType.UNKNOWN
            or job.experience_level == ExperienceLevel.UNKNOWN
            or (job.graduation_requirement is None and "graduat" in raw_job.raw_content.lower())
        )

    async def enrich_if_needed(self, job: NormalizedJob, raw_job: RawJob) -> NormalizedJob:
        """Invokes LLM only if ambiguous fields remain unresolved deterministically."""
        if not self.needs_enrichment(job, raw_job):
            return job
        if not self.enabled:
            self.skipped_no_provider += 1
            return job

        try:
            hits_before = self._cache_hits()
            self.calls += 1
            extractions = await self.provider.extract_ambiguous_fields(job.title, raw_job.raw_content)
            hits_after = self._cache_hits()
            if hits_after > hits_before:
                self.cache_hits += hits_after - hits_before
                self.calls -= 1  # served from cache: no model was called
            # Provenance: what the gateway did, never the prompt or raw output.
            meta = getattr(self.provider, "last_metadata", None)
            if meta:
                job.extraction_metadata["ai"] = meta
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

    def _cache_hits(self) -> int:
        return int(getattr(self.provider, "hits", 0) or 0)
