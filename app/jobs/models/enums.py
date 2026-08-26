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
    OTHER = "OTHER"
