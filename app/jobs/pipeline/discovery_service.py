"""Job discovery service orchestrating the ingestion pipeline."""

import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Type

from sqlalchemy.orm import Session

from app.jobs.database.models import DiscoveryRunRow
from app.jobs.deduplication.deduplicator import JobDeduplicator
from app.jobs.extraction.llm_fallback import LLMFallbackExtractor
from app.jobs.models.enums import JobSourceType
from app.jobs.normalization.normalizer import JobNormalizer
from app.jobs.sources.ashby import AshbySource
from app.jobs.sources.base import JobSource, SourceError
from app.jobs.sources.greenhouse import GreenhouseSource
from app.jobs.sources.lever import LeverSource

logger = logging.getLogger(__name__)


class JobDiscoveryService:
    """Orchestrates end-to-end job discovery, normalization, deduplication, and persistence."""

    def __init__(
        self,
        normalizer: Optional[JobNormalizer] = None,
        deduplicator: Optional[JobDeduplicator] = None,
        llm_fallback: Optional[LLMFallbackExtractor] = None,
    ):
        self.normalizer = normalizer or JobNormalizer()
        self.deduplicator = deduplicator or JobDeduplicator()
        self.llm_fallback = llm_fallback or LLMFallbackExtractor()

        # Source adapter registry
        self._sources: Dict[JobSourceType, Type[JobSource]] = {
            JobSourceType.GREENHOUSE: GreenhouseSource,
            JobSourceType.LEVER: LeverSource,
            JobSourceType.ASHBY: AshbySource,
        }

    def register_source(self, source_type: JobSourceType, source_cls: Type[JobSource]) -> None:
        """Extensibility point for registering custom or future source adapters."""
        self._sources[source_type] = source_cls

    def get_source_adapter(self, source_type: JobSourceType) -> JobSource:
        """Instantiates the source adapter for the given type."""
        source_cls = self._sources.get(source_type)
        if not source_cls:
            raise ValueError(f"Unsupported job source type: {source_type}")
        return source_cls()

    async def run_discovery(
        self,
        db: Session,
        source_type: JobSourceType,
        identifier: str,
        company_name: Optional[str] = None,
    ) -> DiscoveryRunRow:
        """Executes a full discovery run for a specific source identifier."""
        start_time = time.time()
        now = datetime.utcnow()
        identifier = identifier.strip()

        run_row = DiscoveryRunRow(
            source=source_type.value,
            source_identifier=identifier,
            started_at=now,
            status="running",
            errors=[],
        )
        db.add(run_row)
        db.commit()

        logger.info("Starting discovery run for %s (%s)", source_type.value, identifier)
        collected_errors: List[str] = []

        try:
            adapter = self.get_source_adapter(source_type)
            raw_jobs = await adapter.discover(identifier)
            run_row.candidates_discovered = len(raw_jobs)
            run_row.pages_fetched = len(raw_jobs)

            for raw_job in raw_jobs:
                try:
                    # 1. Normalize RawJob -> NormalizedJob
                    normalized = self.normalizer.normalize(raw_job, company_name=company_name)

                    # 2. LLM Fallback (if ambiguous and provider configured)
                    enriched = await self.llm_fallback.enrich_if_needed(normalized, raw_job)

                    # 3. Deduplication & Persistence
                    result = self.deduplicator.process(db, enriched)

                    if result.status == "NEW":
                        run_row.jobs_new += 1
                    elif result.status == "UPDATED":
                        run_row.jobs_updated += 1
                    elif result.status in ("DUPLICATE", "CROSS_SOURCE_DUPLICATE"):
                        run_row.jobs_duplicate += 1

                except Exception as e:
                    run_row.jobs_failed += 1
                    error_msg = f"Job processing error for {raw_job.source_job_id} ({raw_job.raw_title}): {e}"
                    logger.error(error_msg)
                    collected_errors.append(error_msg)

            run_row.status = "completed"

        except SourceError as e:
            run_row.status = "failed"
            error_msg = f"Source adapter error: {e}"
            logger.error(error_msg)
            collected_errors.append(error_msg)
        except Exception as e:
            run_row.status = "failed"
            error_msg = f"Unexpected discovery pipeline failure: {e}"
            logger.error(error_msg)
            collected_errors.append(error_msg)
        finally:
            run_row.errors = collected_errors
            run_row.completed_at = datetime.utcnow()
            run_row.duration_seconds = round(time.time() - start_time, 2)
            db.commit()

        logger.info(
            "Discovery run completed for %s (%s): %d new, %d updated, %d duplicate, %d failed in %.2fs",
            source_type.value,
            identifier,
            run_row.jobs_new,
            run_row.jobs_updated,
            run_row.jobs_duplicate,
            run_row.jobs_failed,
            run_row.duration_seconds or 0,
        )
        return run_row
