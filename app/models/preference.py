"""Preference model."""

from typing import Optional

from pydantic import BaseModel, Field


class Preference(BaseModel):
    target_roles_tier1: list[str] = Field(default_factory=list)
    target_roles_tier2: list[str] = Field(default_factory=list)
    target_roles_lower_priority: list[str] = Field(default_factory=list)
    target_domains: list[str] = Field(default_factory=list)
    #: Also-considered locations beyond the primary target (e.g. "Bengaluru", "Remote").
    preferred_locations: list[str] = Field(default_factory=list)
    #: Job-market target. None means "where my profile says I am" (profile.location),
    #: so the candidate's location is recorded once. See app/jobs/geography.py.
    location_primary: Optional[str] = None
    location_include_country_remote: bool = True
    location_include_other_cities: bool = False
    location_allow_international: bool = False
    #: Postings with no usable location or only "Remote": kept, but UNCERTAIN.
    location_include_unconfirmed: bool = True
    #: Roles outside every target-role family (marketing for an engineer) are not admitted unless this is on.
    role_include_unrelated: bool = False
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
