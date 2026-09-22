"""Extension capture: a job page the candidate saw becomes a RawJob.

The browser extension (Phase 9) will POST a :class:`CapturedJob`. This
adapter turns it into the same ``RawJob`` every other source produces, so a
captured job runs through normalise → hash → dedup → opportunity resolution
exactly like a Greenhouse posting. There is no second creation path.

Field precedence: schema.org ``JobPosting`` JSON-LD first (it is structured
and authored by the employer), then the fields the extension read from the
DOM. Missing title or company is a validation failure, not a guess.
"""

import hashlib
import logging
from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, Field

from app.core.timeutils import parse_timestamp, utc_now
from app.jobs.models.enums import JobSourceType
from app.jobs.models.raw_job import RawJob
from app.jobs.normalization.urls import normalize_url
from app.jobs.sources.base import JobSource

logger = logging.getLogger(__name__)


class CapturedJob(BaseModel):
    """Contract the extension sends to ``POST /api/v2/discovery/capture``."""

    url: str = Field(min_length=8, max_length=2048)
    title: Optional[str] = Field(default=None, max_length=512)
    company: Optional[str] = Field(default=None, max_length=256)
    location: Optional[str] = Field(default=None, max_length=512)
    description: Optional[str] = Field(default=None, description="Page text or HTML of the posting")
    apply_url: Optional[str] = Field(default=None, max_length=2048)
    json_ld: Optional[Any] = Field(default=None, description="schema.org JobPosting object, list or @graph")
    page_metadata: dict[str, Any] = Field(default_factory=dict)
    captured_at: datetime = Field(default_factory=utc_now)
    source_type: JobSourceType = JobSourceType.EXTENSION


def _find_job_posting(payload: Any) -> Optional[dict]:
    """Locate a JobPosting node inside whatever JSON-LD shape the page had."""
    if payload is None:
        return None
    if isinstance(payload, list):
        for item in payload:
            found = _find_job_posting(item)
            if found:
                return found
        return None
    if isinstance(payload, dict):
        node_type = payload.get("@type")
        types = node_type if isinstance(node_type, list) else [node_type]
        if any(str(t).lower() == "jobposting" for t in types if t):
            return payload
        if "@graph" in payload:
            return _find_job_posting(payload["@graph"])
    return None


def _org_name(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        return value.get("name") or value.get("legalName")
    if isinstance(value, str):
        return value
    return None


def _location_text(value: Any) -> Optional[str]:
    if isinstance(value, list):
        parts = [p for p in (_location_text(v) for v in value) if p]
        return "; ".join(parts) or None
    if isinstance(value, dict):
        address = value.get("address")
        if isinstance(address, dict):
            bits = [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")]
            text = ", ".join(str(b) for b in bits if b)
            if text:
                return text
        return value.get("name") or _location_text(address) if address else value.get("name")
    if isinstance(value, str):
        return value
    return None


def company_slug(company: str) -> str:
    return "-".join("".join(ch.lower() if ch.isalnum() else " " for ch in company).split())[:120]


def captured_to_raw_job(capture: CapturedJob) -> tuple[Optional[RawJob], Optional[str]]:
    """Return ``(raw_job, None)`` or ``(None, reason)`` when unusable."""
    posting = _find_job_posting(capture.json_ld)
    posting = posting or {}

    title = (posting.get("title") or capture.title or "").strip()
    company = (_org_name(posting.get("hiringOrganization")) or capture.company or "").strip()
    if not title:
        return None, "missing title"
    if not company:
        return None, "missing company"

    canonical = normalize_url(capture.url) or capture.url
    identifier = posting.get("identifier")
    source_job_id = None
    if isinstance(identifier, dict):
        source_job_id = identifier.get("value")
    elif isinstance(identifier, str):
        source_job_id = identifier
    if not source_job_id:
        source_job_id = "cap-" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]

    description = posting.get("description") or capture.description or ""
    location = _location_text(posting.get("jobLocation")) or capture.location
    workplace = None
    if str(posting.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        workplace = "REMOTE"

    raw = RawJob(
        source=capture.source_type,
        source_job_id=str(source_job_id),
        source_url=capture.url,
        discovered_url=capture.url,
        raw_title=title,
        raw_content=description,
        content_type="html" if "<" in description else "plain",
        raw_location=location,
        source_posted_at=parse_timestamp(posting.get("datePosted")),
        source_deadline=parse_timestamp(posting.get("validThrough")),
        raw_metadata={
            "company_slug": company_slug(company),
            "captured": True,
            "captured_at": capture.captured_at.isoformat(),
            "apply_url": capture.apply_url,
            "workplaceType": workplace,
            "employmentType": posting.get("employmentType"),
            "page_metadata": dict(capture.page_metadata),
            "json_ld_present": bool(posting),
        },
        discovered_at=capture.captured_at,
        retrieved_at=capture.captured_at,
        extraction_method="extension_capture",
    )
    return raw, None


class CaptureSource(JobSource):
    """Adapter that wraps a single captured page. Never touches the network."""

    @property
    def source_type(self) -> JobSourceType:
        return JobSourceType.EXTENSION

    async def discover(self, identifier: str, **kwargs) -> List[RawJob]:
        capture: Optional[CapturedJob] = kwargs.get("capture")
        if capture is None:
            return []
        raw, reason = captured_to_raw_job(capture)
        if raw is None:
            logger.info("Capture rejected (%s): %s", reason, capture.url)
            return []
        return [raw]
