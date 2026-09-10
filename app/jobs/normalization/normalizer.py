"""Job normalization layer: converts RawJob into canonical NormalizedJob."""

import hashlib
import logging
import re
from typing import List, Optional, Tuple

from app.core.timeutils import utc_now
from app.jobs.extraction.deterministic import (
    clean_html,
    extract_bullets_and_sections,
    extract_employment_type,
    extract_experience_level,
    extract_graduation_requirement,
    extract_remote_type,
    extract_salary,
    extract_skills_and_technologies,
)
from app.jobs.models.enums import JobStatus, ProcessingStatus
from app.jobs.models.job import NormalizedJob
from app.jobs.models.raw_job import RawJob

logger = logging.getLogger(__name__)


def compute_content_hash(company: str, title: str, description: str) -> str:
    """Computes a stable SHA-256 hash of the core content.
    
    Insensitive to superficial whitespace or formatting variations,
    but sensitive to meaningful wording, responsibility, or qualification changes.
    """
    clean_desc = " ".join(clean_html(description).lower().split())
    canonical_text = f"{company.lower().strip()}|{title.lower().strip()}|{clean_desc}"
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()


def compute_canonical_key(company: str, normalized_title: str, location: Optional[str] = None) -> str:
    """Computes a deterministic canonical identity key for deduplication."""
    norm_comp = company.lower().strip()
    norm_tit = normalized_title.lower().strip()
    norm_loc = (location or "").lower().strip()
    raw_key = f"{norm_comp}|{norm_tit}|{norm_loc}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


# Board slugs are lowercase and punctuation-free; title-casing them alone
# produced "Acme" for a company that calls itself "ACME Inc". Known acronyms and
# suffixes are preserved so the canonical key is stable across sources.
_COMPANY_ACRONYMS = {"ai", "api", "io", "hq", "ibm", "aws", "gcp", "ml", "hr", "it", "us", "uk"}
_COMPANY_SUFFIXES = {"inc", "llc", "ltd", "gmbh", "bv", "plc", "corp", "co"}


def normalize_company(raw_company: str) -> str:
    """Normalize a company name or board slug into a stable display form."""
    cleaned = re.sub(r"[_\-]+", " ", str(raw_company or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return "Unknown Company"

    words = []
    for word in cleaned.split(" "):
        lowered = word.lower().strip(".,")
        if lowered in _COMPANY_ACRONYMS:
            words.append(lowered.upper())
        elif lowered in _COMPANY_SUFFIXES:
            words.append(lowered.capitalize())
        elif any(ch.isupper() for ch in word[1:]):
            # Already deliberately cased (e.g. "GitLab", "OpenAI") - keep as-is.
            words.append(word)
        else:
            words.append(word.capitalize())
    return " ".join(words)


def normalize_title(raw_title: str, company: Optional[str] = None) -> str:
    """Cleans and standardizes the job title.
    
    Strips leading company names, brackets, and job codes.
    Example: 'Stripe - Backend Engineer (Remote)' -> 'Backend Engineer'
    """
    title = raw_title.strip()

    # Remove leading company prefix if present (e.g. "Stripe - ", "Uber | ")
    if company:
        comp_pattern = re.escape(company)
        title = re.sub(rf"^{comp_pattern}\s*[-|–:]\s*", "", title, flags=re.IGNORECASE).strip()

    # Strip bracketed prefixes like [Remote], (Full-time), [REQ-123]
    title = re.sub(r"^[\[\(][^\]\)]+[\]\)]\s*", "", title).strip()

    # Strip trailing bracketed location/status like (Remote), [Hybrid]
    title = re.sub(r"\s*[\[\(](?:Remote|Hybrid|On-site|Onsite|Full[\s-]Time|Internship)[\]\)]\s*$", "", title, flags=re.IGNORECASE).strip()

    return title if title else raw_title.strip()


def normalize_location(raw_location: Optional[str]) -> Tuple[Optional[str], List[str]]:
    """Normalizes raw location string into a primary location and a list of locations."""
    if not raw_location or not raw_location.strip():
        return None, []

    loc_str = raw_location.strip()
    # Split multiple locations separated by ';', '/', or ' | '
    parts = [p.strip() for p in re.split(r"[;/|]", loc_str) if p.strip()]
    if not parts:
        return loc_str, [loc_str]

    primary = parts[0]
    return primary, parts


class JobNormalizer:
    """Normalizes RawJob into a strongly typed NormalizedJob."""

    def normalize(self, raw_job: RawJob, company_name: Optional[str] = None) -> NormalizedJob:
        """Converts a RawJob into a NormalizedJob using deterministic extraction."""
        source_identifier = (
            raw_job.raw_metadata.get("board_token")
            or raw_job.raw_metadata.get("company_slug")
            or raw_job.raw_metadata.get("board_name")
        )
        company = company_name or source_identifier or "Unknown Company"
        company = normalize_company(company)

        clean_title = normalize_title(raw_job.raw_title, company)
        primary_location, locations = normalize_location(raw_job.raw_location)

        plain_description = clean_html(raw_job.raw_content)
        emp_type = extract_employment_type(raw_job.raw_title, raw_job.raw_content, raw_job.raw_metadata)
        remote_type = extract_remote_type(raw_job.raw_title, raw_job.raw_location, raw_job.raw_content, raw_job.raw_metadata)
        exp_level = extract_experience_level(raw_job.raw_title, raw_job.raw_content)
        grad_req = extract_graduation_requirement(raw_job.raw_content)
        salary = extract_salary(raw_job.raw_content, raw_job.raw_metadata)
        skills, tech = extract_skills_and_technologies(raw_job.raw_content)
        responsibilities, qualifications = extract_bullets_and_sections(raw_job.raw_content)

        content_hash = compute_content_hash(company, clean_title, raw_job.raw_content)
        canonical_key = compute_canonical_key(company, clean_title, primary_location)

        apply_url = raw_job.raw_metadata.get("apply_url") or raw_job.raw_metadata.get("applyUrl")

        return NormalizedJob(
            canonical_key=canonical_key,
            source=raw_job.source,
            source_job_id=raw_job.source_job_id,
            source_identifier=source_identifier,
            company=company,
            title=clean_title,
            original_title=raw_job.raw_title,
            description=plain_description,
            original_description=raw_job.raw_content,
            location=primary_location,
            locations=locations,
            remote_type=remote_type,
            employment_type=emp_type,
            experience_level=exp_level,
            graduation_requirement=grad_req,
            graduation_year_requirement=grad_req.minimum_year if grad_req else None,
            required_skills=skills,
            preferred_skills=[],
            technologies=tech,
            responsibilities=responsibilities,
            qualifications=qualifications,
            salary_text=salary,
            application_url=apply_url,
            source_url=raw_job.source_url,
            posted_at=raw_job.source_posted_at,
            source_updated_at=raw_job.source_updated_at,
            deadline=raw_job.source_deadline,
            first_seen_at=raw_job.discovered_at,
            last_seen_at=raw_job.retrieved_at or raw_job.discovered_at,
            content_hash=content_hash,
            processing_status=ProcessingStatus.NORMALIZED,
            job_status=JobStatus.ACTIVE,
            extraction_metadata={
                "method": raw_job.extraction_method or "deterministic",
                "extracted_at": utc_now().isoformat(),
            },
            metadata=raw_job.raw_metadata,
        )
