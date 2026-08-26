"""LLM Provider abstraction for structured extraction fallback."""

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class LLMProvider(ABC):
    """Abstract interface for LLM extraction providers."""

    @abstractmethod
    async def extract_ambiguous_fields(self, title: str, content: str) -> Dict[str, Any]:
        """Extracts structured fields from job posting text for ambiguous cases.
        
        Returns a dict that may contain:
        - employment_type: str
        - remote_type: str
        - experience_level: str
        - graduation_requirement: dict
        - salary_text: str
        """
        pass


class StubProvider(LLMProvider):
    """Offline stub provider for unit tests and default keyless operation."""

    async def extract_ambiguous_fields(self, title: str, content: str) -> Dict[str, Any]:
        logger.debug("StubProvider called for '%s' — returning empty fallback", title)
        return {}


class GeminiProvider(LLMProvider):
    """Google Gemini provider for semantic extraction fallback."""

    GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"

    def __init__(self, api_key: Optional[str] = None, timeout: float = 15.0):
        self.api_key = api_key or settings.gemini_api_key
        self.timeout = timeout

    async def extract_ambiguous_fields(self, title: str, content: str) -> Dict[str, Any]:
        if not self.api_key:
            logger.warning("Gemini API key not configured, falling back to empty extraction")
            return {}

        prompt = f"""You are a job description parser. Extract structured fields from the job description below.
Return ONLY valid JSON matching this schema:
{{
  "employment_type": "INTERNSHIP" | "FULL_TIME" | "PART_TIME" | "CONTRACT" | "UNKNOWN",
  "remote_type": "REMOTE" | "HYBRID" | "ON_SITE" | "UNKNOWN",
  "experience_level": "INTERN" | "ENTRY_LEVEL" | "JUNIOR" | "MID" | "SENIOR" | "UNKNOWN",
  "graduation_minimum_year": null | integer,
  "graduation_maximum_year": null | integer,
  "salary_text": null | string
}}

Job Title: {title}
Job Description (first 2500 chars):
{content[:2500]}
"""

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.GEMINI_API_URL}?key={self.api_key}",
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
        except Exception as e:
            logger.warning("Gemini extraction failed for '%s': %s", title, e)
            return {}


def get_llm_provider() -> LLMProvider:
    """Factory for selecting LLMProvider based on configuration."""
    if settings.llm_provider == "gemini" and settings.gemini_api_key:
        return GeminiProvider()
    return StubProvider()
