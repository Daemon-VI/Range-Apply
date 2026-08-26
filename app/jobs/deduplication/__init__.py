"""Deduplication package."""

from app.jobs.deduplication.deduplicator import (
    DeduplicationResult,
    JobDeduplicator,
    clean_url,
)

__all__ = [
    "DeduplicationResult",
    "JobDeduplicator",
    "clean_url",
]
