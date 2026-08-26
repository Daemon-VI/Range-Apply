"""Achievement model."""

from typing import Optional

from pydantic import BaseModel, Field

from app.models.enums import VerificationStatus


class Achievement(BaseModel):
    id: str
    title: str
    category: str
    description: str = Field(default="")
    date: Optional[str] = None
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    related_project: Optional[str] = None
    notes: str = Field(default="")

    @property
    def is_verified(self) -> bool:
        return self.verification_status == VerificationStatus.VERIFIED
