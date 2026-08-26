"""Abstract base class for all job source adapters."""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from app.jobs.models.enums import JobSourceType
from app.jobs.models.raw_job import RawJob


class SourceError(Exception):
    """Raised when a job source fails to discover or fetch jobs."""

    def __init__(self, message: str, source: JobSourceType, identifier: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.source = source
        self.identifier = identifier
        self.status_code = status_code


class JobSource(ABC):
    """Abstract interface for all job source adapters.
    
    Each adapter encapsulates its own discovery and fetching logic,
    returning uniform RawJob instances without core pipeline branching.
    """

    @property
    @abstractmethod
    def source_type(self) -> JobSourceType:
        """Returns the JobSourceType for this adapter."""
        pass

    @abstractmethod
    async def discover(self, identifier: str, **kwargs) -> List[RawJob]:
        """Discovers and retrieves raw job postings for the given identifier.
        
        Args:
            identifier: Board token, company slug, or endpoint identifier.
            **kwargs: Optional adapter-specific parameters.
            
        Returns:
            List of RawJob instances preserving all original content and metadata.
        """
        pass
