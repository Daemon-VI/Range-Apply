"""Greenhouse job source adapter using the public unauthenticated Job Board API."""

import html
import logging
from typing import List, Optional

from app.core.timeutils import parse_timestamp, utc_now
from app.jobs.models.enums import JobSourceType
from app.jobs.models.raw_job import RawJob
from app.jobs.sources.base import JobSource

logger = logging.getLogger(__name__)


class GreenhouseSource(JobSource):
    """Fetches job listings from Greenhouse's public JSON API.

    Endpoint: https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
    Zero authentication required, free tier compliant, structured JSON with full HTML JD.
    """

    BASE_URL = "https://boards-api.greenhouse.io/v1/boards"

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

        data = await self.fetch_json(url, board_token)

        raw_jobs: List[RawJob] = []
        jobs_list = data.get("jobs", []) if isinstance(data, dict) else []
        retrieved_at = utc_now()

        for item in jobs_list:
            job_id = str(item.get("id", ""))
            if not job_id:
                continue

            # A null title is a malformed record (rejected by validation), never a crash of the whole board.
            title = (item.get("title") or "").strip()
            absolute_url = item.get(
                "absolute_url", f"https://boards.greenhouse.io/{board_token}/jobs/{job_id}"
            )
            # Audit fix (2026-09-14): the Job Board API returns `content` entity-escaped
            # ("&lt;h3&gt;..." on the live highradius board). Kept escaped, clean_html turned
            # it back into literal tags inside the stored description and no <li> was ever
            # found, so no Greenhouse posting had qualifications/responsibilities.
            content = html.unescape(item.get("content") or "")

            # Extract location string
            location_raw = item.get("location")
            location_str: Optional[str] = None
            if isinstance(location_raw, dict):
                location_str = location_raw.get("name")
            elif isinstance(location_raw, str):
                location_str = location_raw

            # Greenhouse exposes `first_published` on most boards and always
            # `updated_at`. Fall back to updated_at only for the posted date,
            # never invent one.
            posted_at = parse_timestamp(item.get("first_published")) or parse_timestamp(
                item.get("updated_at")
            )
            updated_at = parse_timestamp(item.get("updated_at"))

            raw_job = RawJob(
                source=self.source_type,
                source_job_id=job_id,
                source_url=absolute_url,
                discovered_url=discovered_url,
                raw_title=title,
                raw_content=content,
                content_type="html",
                raw_location=location_str,
                source_posted_at=posted_at,
                source_updated_at=updated_at,
                raw_metadata={
                    "board_token": board_token,
                    "departments": item.get("departments", []),
                    "offices": item.get("offices", []),
                    "updated_at": item.get("updated_at"),
                    "first_published": item.get("first_published"),
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
