"""Greenhouse job source adapter using the public unauthenticated Job Board API."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from app.jobs.models.enums import JobSourceType
from app.jobs.models.raw_job import RawJob
from app.jobs.sources.base import JobSource, SourceError

logger = logging.getLogger(__name__)


class GreenhouseSource(JobSource):
    """Fetches job listings from Greenhouse's public JSON API.
    
    Endpoint: https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
    Zero authentication required, free tier compliant, structured JSON with full HTML JD.
    """

    BASE_URL = "https://boards-api.greenhouse.io/v1/boards"

    def __init__(self, timeout: float = 15.0, client: Optional[httpx.AsyncClient] = None):
        self.timeout = timeout
        self._client = client

    @property
    def source_type(self) -> JobSourceType:
        return JobSourceType.GREENHOUSE

    async def discover(self, identifier: str, **kwargs) -> List[RawJob]:
        """Discovers all open jobs on a Greenhouse job board.
        
        Args:
            identifier: The Greenhouse board token (e.g. 'stripe', 'cloudflare', 'figma').
        """
        board_token = identifier.strip()
        url = f"{self.BASE_URL}/{board_token}/jobs?content=true"
        discovered_url = f"https://boards.greenhouse.io/{board_token}"

        should_close_client = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout)
            should_close_client = True

        try:
            response = await client.get(url)
            if response.status_code == 404:
                raise SourceError(
                    f"Greenhouse board not found: '{board_token}'",
                    source=self.source_type,
                    identifier=board_token,
                    status_code=404,
                )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as e:
            raise SourceError(
                f"Greenhouse HTTP error {e.response.status_code} for board '{board_token}': {e}",
                source=self.source_type,
                identifier=board_token,
                status_code=e.response.status_code,
            ) from e
        except httpx.RequestError as e:
            raise SourceError(
                f"Greenhouse request failed for board '{board_token}': {e}",
                source=self.source_type,
                identifier=board_token,
            ) from e
        finally:
            if should_close_client:
                await client.aclose()

        raw_jobs: List[RawJob] = []
        jobs_list = data.get("jobs", [])
        retrieved_at = datetime.utcnow()

        for item in jobs_list:
            job_id = str(item.get("id", ""))
            if not job_id:
                continue

            title = item.get("title", "").strip()
            absolute_url = item.get(
                "absolute_url", f"https://boards.greenhouse.io/{board_token}/jobs/{job_id}"
            )
            content = item.get("content", "") or ""

            # Extract location string
            location_raw = item.get("location")
            location_str: Optional[str] = None
            if isinstance(location_raw, dict):
                location_str = location_raw.get("name")
            elif isinstance(location_raw, str):
                location_str = location_raw

            raw_job = RawJob(
                source=self.source_type,
                source_job_id=job_id,
                source_url=absolute_url,
                discovered_url=discovered_url,
                raw_title=title,
                raw_content=content,
                content_type="html",
                raw_location=location_str,
                raw_metadata={
                    "board_token": board_token,
                    "departments": item.get("departments", []),
                    "offices": item.get("offices", []),
                    "updated_at": item.get("updated_at"),
                    "requisition_id": item.get("requisition_id"),
                    "metadata": item.get("metadata", []),
                },
                discovered_at=retrieved_at,
                retrieved_at=retrieved_at,
                extraction_method="greenhouse_api",
            )
            raw_jobs.append(raw_job)

        logger.info(
            "Greenhouse discovered %d jobs for board token '%s'",
            len(raw_jobs),
            board_token,
        )
        return raw_jobs
