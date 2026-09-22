"""Lever job source adapter using the public unauthenticated Postings API."""

import html
import logging
from typing import List

from app.core.timeutils import parse_timestamp, utc_now
from app.jobs.models.enums import JobSourceType
from app.jobs.models.raw_job import RawJob
from app.jobs.sources.base import JobSource

logger = logging.getLogger(__name__)


class LeverSource(JobSource):
    """Fetches job listings from Lever's public JSON API.

    Endpoint: https://api.lever.co/v0/postings/{company_slug}?mode=json
    Zero authentication required, free tier compliant, structured JSON with full HTML JD.
    """

    BASE_URL = "https://api.lever.co/v0/postings"

    @property
    def source_type(self) -> JobSourceType:
        return JobSourceType.LEVER

    async def discover(self, identifier: str, **kwargs) -> List[RawJob]:
        """Discovers all open jobs on a Lever postings board.

        Args:
            identifier: The company slug (e.g. 'leverdemo', 'netflix', 'palantir').
        """
        company_slug = identifier.strip()
        url = f"{self.BASE_URL}/{company_slug}?mode=json"
        discovered_url = f"https://jobs.lever.co/{company_slug}"

        data = await self.fetch_json(url, company_slug)

        if not isinstance(data, list):
            data = []

        raw_jobs: List[RawJob] = []
        retrieved_at = utc_now()

        for item in data:
            job_id = str(item.get("id", ""))
            if not job_id:
                continue

            title = (item.get("text") or "").strip() or (item.get("title") or "").strip()
            # The live v0 API puts `hostedUrl` / `applyUrl` at the top level;
            # older payloads (and the docs' examples) nest them under `urls`.
            # The application form lives at the `/apply` page, not the posting.
            urls = item.get("urls") or {}
            show_url = item.get("hostedUrl") or urls.get("show") or f"https://jobs.lever.co/{company_slug}/{job_id}"
            apply_url = item.get("applyUrl") or urls.get("apply") or f"{show_url.rstrip('/')}/apply"

            content = self._content(item)
            content_type = "html" if (item.get("descriptionHtml") or item.get("description") or item.get("lists") or item.get("additional")) else "plain"

            categories = item.get("categories", {})
            location_str = categories.get("location") if isinstance(categories, dict) else None

            # Lever reports `createdAt` as epoch milliseconds.
            posted_at = parse_timestamp(item.get("createdAt"))
            updated_at = parse_timestamp(item.get("updatedAt"))

            raw_job = RawJob(
                source=self.source_type,
                source_job_id=job_id,
                source_url=show_url,
                discovered_url=discovered_url,
                raw_title=title,
                raw_content=content,
                content_type=content_type,
                raw_location=location_str,
                source_posted_at=posted_at,
                source_updated_at=updated_at,
                raw_metadata={
                    "company_slug": company_slug,
                    "categories": categories,
                    "workplaceType": item.get("workplaceType"),
                    "commitment": categories.get("commitment")
                    if isinstance(categories, dict)
                    else None,
                    "apply_url": apply_url,
                    "salaryRange": item.get("salaryRange"),
                    "createdAt": item.get("createdAt"),
                    "lists": item.get("lists", []),
                },
                discovered_at=retrieved_at,
                retrieved_at=retrieved_at,
                extraction_method="lever_api",
            )
            raw_jobs.append(raw_job)

        logger.info(
            "Lever discovered %d jobs for company slug '%s'",
            len(raw_jobs),
            company_slug,
        )
        return raw_jobs

    @staticmethod
    def _content(item: dict) -> str:
        """The whole posting: description, then every titled list, then the closing section.

        Audit fix (2026-09-14): the v0 API's ``description`` is only the opening;
        responsibilities and requirements live in ``lists`` ("Experience and
        Qualification") and ``additional``. On the live Zeta board 18 of 18
        postings stated years of experience only there, so eligibility, skills
        and qualification extraction never saw them.
        """
        body = item.get("descriptionHtml") or item.get("description") or item.get("descriptionPlain") or ""
        parts = [body] if body.strip() else []
        for section in item.get("lists") or []:
            if not isinstance(section, dict):
                continue
            entries = section.get("content") or ""
            if not isinstance(entries, str) or not entries.strip():
                continue
            heading = (section.get("text") or "").strip()
            parts.append((f"<h3>{html.escape(heading)}</h3>" if heading else "") + f"<ul>{entries}</ul>")
        additional = item.get("additional") or item.get("additionalPlain") or ""
        if isinstance(additional, str) and additional.strip():
            parts.append(additional)
        return "\n".join(parts)
