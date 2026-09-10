"""Adapters between persistence rows and intelligence domain models."""

from app.intelligence.adapters.job_adapter import job_row_to_normalized

__all__ = ["job_row_to_normalized"]
