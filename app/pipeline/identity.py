"""Opportunity identity: which source job rows are the same real-world opening.

This sits *above* ``JobDeduplicator``. The deduplicator decides whether two
source records are the same *posting* (exact source id / URL / canonical key /
content hash). Opportunity identity decides whether two postings are the same
*hiring need*: the same company, the same normalised title, in the same
location bucket — deliberately coarser than ``canonical_key`` (which keeps the
raw location string) so "Remote", "Remote - US" and "Remote (India)" converge,
while "Bangalore" and "Hyderabad" do not.

Every level is exact equality on a normalised key. There is no fuzzy merge:
when the key differs, it is a different opportunity, and two rows are always
safer than one wrong merge (invariant carried over from the deduplicator).
"""

import hashlib
import re
from dataclasses import dataclass

from app.jobs.database.models import JobRow

_PARENTHETICAL_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*")
_JOB_CODE_RE = re.compile(r"\s*[-–|#]\s*(?:req|job|id|r)?[\s#:]*[a-z]{0,3}\d{3,}\s*$", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_REMOTE_HINTS = ("remote", "anywhere", "work from home", "wfh", "distributed")


@dataclass(frozen=True)
class OpportunityIdentity:
    company: str
    title: str
    location_bucket: str

    @property
    def key(self) -> str:
        raw = f"{self.company}|{self.title}|{self.location_bucket}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_title_for_identity(title: str) -> str:
    text = _PARENTHETICAL_RE.sub(" ", title or "")
    text = _JOB_CODE_RE.sub("", text)
    text = _WS_RE.sub(" ", text).strip().lower()
    return text


def location_bucket(location: str | None, remote_type: str | None) -> str:
    """Coarse location: ``remote`` or the first comma-separated segment (city)."""
    text = (location or "").strip().lower()
    if (remote_type or "").upper() == "REMOTE" or any(h in text for h in _REMOTE_HINTS):
        return "remote"
    if not text:
        return ""
    first = text.split(",")[0]
    first = _PARENTHETICAL_RE.sub(" ", first)
    return _WS_RE.sub(" ", first).strip()


def identity_for_job(job: JobRow) -> OpportunityIdentity:
    return OpportunityIdentity(
        company=_WS_RE.sub(" ", (job.company or "").strip().lower()),
        title=normalize_title_for_identity(job.title or job.original_title or ""),
        location_bucket=location_bucket(job.location, job.remote_type),
    )
