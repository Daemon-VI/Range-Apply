"""Ashby job source adapter using the public unauthenticated Posting API."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from app.jobs.models.enums import JobSourceType
from app.jobs.models.raw_job import RawJob
from app.jobs.sources.base import JobSource, SourceError

logger = logging.getLogger(__name__)


class AshbySource(JobSource):
    """Fetches job listings from Ashby's public JSON Posting API.
    
    Endpoint: https://api.ashbyhq.com/posting-api/job-board/{board_name}?includeCompensation=true
    Zero authentication required, free tier compliant, structured JSON with full HTML/plain JD.
    """

    BASE_URL = "https://api.ashbyhq.com/posting-api/job-board"

    def __init__(self, timeout: float = 15.0, client: Optional[httpx.AsyncClient] = None):
        self.timeout = timeout
        self._client = client

    @property
    def source_type(self) -> JobSourceType:
        return JobSourceType.ASHBY

    async def discover(self, identifier: str, **kwargs) -> List[RawJob]:
        """Discovers all open jobs on an Ashby job board.
        
        Args:
            identifier: The Ashby board name (e.g. 'ashby', 'ramp', 'retool').
        """
        board_name = identifier.strip()
        url = f"{self.BASE_URL}/{board_name}?includeCompensation=true"
        discovered_url = f"https://jobs.ashbyhq.com/{board_name}"

        should_close_client = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout)
            should_close_client = True

        try:
            response = await client.get(url)
            if response.status_code == 404:
                raise SourceError(
                    f"Ashby job board not found for name: '{board_name}'",
                    source=self.source_type,
                    identifier=board_name,
                    status_code=404,
                )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as e:
            raise SourceError(
                f"Ashby HTTP error {e.response.status_code} for board '{board_name}': {e}",
                source=self.source_type,
                identifier=board_name,
                status_code=e.response.status_code,
            ) from e
        except httpx.RequestError as e:
            raise SourceError(
                f"Ashby request failed for board '{board_name}': {e}",
                source=self.source_type,
                identifier=board_name,
            ) from e
        finally:
            if should_close_client:
                await client.aclose()

        jobs_list = data.get("jobs", []) if isinstance(data, dict) else []
        raw_jobs: List[RawJob] = []
        retrieved_at = datetime.utcnow()

        for item in jobs_list:
            job_id = str(item.get("id", ""))
            if not job_id:
                continue

            title = item.get("title", "").strip()
            job_url = item.get("jobUrl") or f"https://jobs.ashbyhq.com/{board_name}/{job_id}"
            apply_url = item.get("applyUrl")

            content = item.get("descriptionHtml") or item.get("descriptionPlain") or ""
            content_type = "html" if item.get("descriptionHtml") else "plain"
            location_str = item.get("location")

            raw_job = RawJob(
                source=self.source_type,
                source_job_id=job_id,
                source_url=job_url,
                discovered_url=discovered_url,
                raw_title=title,
                raw_content=content,
                content_type=content_type,
                raw_location=location_str,
                raw_metadata={
                    "board_name": board_name,
                    "department": item.get("department"),
                    "team": item.get("team"),
                    "employmentType": item.get("employmentType"),
                    "workplaceType": item.get("workplaceType"),
                    "secondaryLocations": item.get("secondaryLocations", []),
                    "compensation": item.get("compensation"),
                    "compensationTierSummary": item.get("compensationTierSummary"),
                    "applyUrl": apply_url,
                    "publishedAt": item.get("publishedAt"),
                },
                discovered_at=retrieved_at,
                retrieved_at=retrieved_at,
                extraction_method="ashby_api",
            )
            raw_jobs.append(raw_job)

        logger.info(
            "Ashby discovered %d jobs for board '%s'",
            len(raw_jobs),
            board_name,
        )
        return raw_jobs
