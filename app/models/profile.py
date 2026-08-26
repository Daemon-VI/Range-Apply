"""Profile model."""

from typing import Optional

from pydantic import BaseModel, Field


class Profile(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    work_authorization: Optional[str] = None
    degree: str
    branch: str
    college: str
    graduation_year: int
    current_academic_status: str
    cgpa: float
    backlogs: str
    github: Optional[str] = None
    linkedin: Optional[str] = None
    portfolio: Optional[str] = None
    positioning_statement: str = Field(default="")
    long_term_goal: str = Field(default="")
