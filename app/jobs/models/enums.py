"""Enumerations for job domain models."""

from enum import Enum


class EmploymentType(str, Enum):
    INTERNSHIP = "INTERNSHIP"
    FULL_TIME = "FULL_TIME"
    PART_TIME = "PART_TIME"
    CONTRACT = "CONTRACT"
    UNKNOWN = "UNKNOWN"


class RemoteType(str, Enum):
    REMOTE = "REMOTE"
    HYBRID = "HYBRID"
    ON_SITE = "ON_SITE"
    UNKNOWN = "UNKNOWN"


class JobStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class ProcessingStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    FETCHED = "FETCHED"
    EXTRACTED = "EXTRACTED"
    NORMALIZED = "NORMALIZED"
    VALIDATED = "VALIDATED"
    FAILED = "FAILED"


class ExperienceLevel(str, Enum):
    INTERN = "INTERN"
    ENTRY_LEVEL = "ENTRY_LEVEL"
    JUNIOR = "JUNIOR"
    MID = "MID"
    SENIOR = "SENIOR"
    UNKNOWN = "UNKNOWN"


class JobSourceType(str, Enum):
    GREENHOUSE = "GREENHOUSE"
    LEVER = "LEVER"
    ASHBY = "ASHBY"
    COMPANY_CAREER_PAGE = "COMPANY_CAREER_PAGE"
    #: A page the candidate saw in their browser, captured by the extension.
    EXTENSION = "EXTENSION"
    #: A public aggregator / RSS feed (adapters arrive with later phases).
    AGGREGATOR = "AGGREGATOR"
    OTHER = "OTHER"


class Freshness(str, Enum):
    """Deterministic age classification of a posting; never invented dates."""

    FRESH = "FRESH"
    RECENT = "RECENT"
    AGING = "AGING"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"
