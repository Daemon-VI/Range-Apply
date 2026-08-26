"""Skill model."""

from pydantic import BaseModel, Field

from app.models.enums import SkillCategory, VerificationStatus


class Skill(BaseModel):
    id: str
    name: str
    category: SkillCategory
    evidence: str = Field(default="")
    projects: list[str] = Field(default_factory=list)
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    resume_relevance: str = Field(default="medium")
    notes: str = Field(default="")

    @property
    def is_verified(self) -> bool:
        return self.verification_status == VerificationStatus.VERIFIED

    @property
    def allowed_for_resume(self) -> bool:
        return self.verification_status == VerificationStatus.VERIFIED

    @property
    def allowed_for_application(self) -> bool:
        return self.verification_status == VerificationStatus.VERIFIED
