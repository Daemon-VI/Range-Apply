"""CareerFact model — atomic verified career claim."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.core.timeutils import utc_now

from app.models.enums import FactCategory, VerificationStatus


class CareerFact(BaseModel):
    id: str
    category: FactCategory
    statement: str
    source: str = Field(default="RIBHU_CAREER_CONTEXT.md")
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    allowed_for_resume: bool = False
    allowed_for_application: bool = False
    related_entity_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def model_post_init(self, __context: object) -> None:
        if self.verification_status == VerificationStatus.VERIFIED:
            if not self.allowed_for_resume:
                object.__setattr__(self, "allowed_for_resume", True)
            if not self.allowed_for_application:
                object.__setattr__(self, "allowed_for_application", True)
