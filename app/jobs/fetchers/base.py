"""Content fetcher abstraction for web content retrieval."""

from abc import ABC, abstractmethod
from typing import Dict, Optional

from pydantic import BaseModel, Field


class FetchResult(BaseModel):
    """Result of a content fetch operation."""

    url: str
    content: str = ""
    content_type: str = "html"  # "html" | "markdown" | "json"
    status_code: int = 200
    metadata: Dict = Field(default_factory=dict)
    success: bool = True
    error: Optional[str] = None


class ContentFetcher(ABC):
    """Abstract interface for fetching web content."""

    @abstractmethod
    async def fetch(self, url: str, **kwargs) -> FetchResult:
        """Fetches web page content.
        
        Args:
            url: The public web page URL to fetch.
            
        Returns:
            FetchResult containing the raw/extracted content and metadata.
        """
        pass
