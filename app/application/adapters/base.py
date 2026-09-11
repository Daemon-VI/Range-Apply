"""Adapter contract for submitting an application to an ATS."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class SubmissionResult:
    success: bool
    confirmation: Optional[str]
    error: Optional[str]
    dry_run: bool


class ATSAdapter(ABC):
    """One implementation per applicant-tracking system."""

    @abstractmethod
    def prepare(
        self,
        job_row,
        profile,
        resume_text: str,
        cover_letter_text: str,
    ) -> Dict[str, Any]:
        """Build the field-name -> value package to submit, from real data only."""
        raise NotImplementedError

    @abstractmethod
    async def submit(
        self, prepared: Dict[str, Any], application_url: str, dry_run: bool
    ) -> SubmissionResult:
        raise NotImplementedError
