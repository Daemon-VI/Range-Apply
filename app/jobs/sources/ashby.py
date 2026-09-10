"""Ashby job source adapter using the public unauthenticated Posting API."""

import logging
from typing import List

from app.core.timeutils import parse_timestamp, utc_now
from app.jobs.models.enums import JobSourceType
from app.jobs.models.raw_job import RawJob
from app.jobs.sources.base import JobSource

logger = logging.getLogger(__name__)


class AshbySource(JobSource):
    """Fetches job listings from Ashby's public JSON Posting API.

    Endpoint: https://api.ashbyhq.com/posting-api/job-board/{board_name}?includeCompensation=true
    Zero authentication required, free tier compliant, structured JSON with full HTML/plain JD.
    """

    BASE_URL = "https://api.ashbyhq.com/posting-api/job-board"

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

        data = await self.fetch_json(url, board_name)

        jobs_list = data.get("jobs", []) if isinstance(data, dict) else []
        raw_jobs: List[RawJob] = []
        retrieved_at = utc_now()

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

            posted_at = parse_timestamp(item.get("publishedAt"))
            updated_at = parse_timestamp(item.get("updatedAt"))

            raw_job = RawJob(
                source=self.source_type,
                source_job_id=job_id,
                source_url=job_url,
                discovered_url=discovered_url,
                raw_title=title,
                raw_content=content,
                content_type=content_type,
                raw_location=location_str,
                source_posted_at=posted_at,
                source_updated_at=updated_at,
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
