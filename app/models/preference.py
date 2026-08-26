"""Preference model."""

from typing import Optional

from pydantic import BaseModel, Field


class Preference(BaseModel):
    target_roles_tier1: list[str] = Field(default_factory=list)
    target_roles_tier2: list[str] = Field(default_factory=list)
    target_roles_lower_priority: list[str] = Field(default_factory=list)
    target_domains: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)
    remote_preference: Optional[str] = None
    hybrid_preference: Optional[str] = None
    internship_preference: bool = True
    full_time_preference: bool = True
    compensation_target: Optional[str] = None
    target_companies: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)
    minimum_match_score: Optional[float] = None
    graduation_eligibility: int = 2027

    @property
    def all_target_roles(self) -> list[str]:
        return (
            self.target_roles_tier1
            + self.target_roles_tier2
            + self.target_roles_lower_priority
        )
