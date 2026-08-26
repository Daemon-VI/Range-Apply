"""Experience model."""

from typing import Optional

from pydantic import BaseModel, Field

from app.models.enums import VerificationStatus


class Experience(BaseModel):
    id: str
    organization: str
    role: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    is_employment: bool = False
    responsibilities: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)
    verified_claims: list[str] = Field(default_factory=list)
    verification_status: VerificationStatus = VerificationStatus.VERIFIED
    notes: str = Field(default="")
