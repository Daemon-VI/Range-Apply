"""Claim model — statement with verification metadata."""

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.models.enums import VerificationStatus

if TYPE_CHECKING:
    from app.models.career_fact import CareerFact


class Claim(BaseModel):
    statement: str
    source: str = Field(default="")
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    allowed_for_resume: bool = False
    allowed_for_application: bool = False
    notes: str = Field(default="")

    @classmethod
    def from_fact(cls, fact: "CareerFact") -> "Claim":
        return cls(
            statement=fact.statement,
            source=fact.source,
            verification_status=fact.verification_status,
            allowed_for_resume=fact.allowed_for_resume,
            allowed_for_application=fact.allowed_for_application,
        )
