"""Skills endpoint."""

from fastapi import APIRouter, Depends

from app.api.deps import get_career_brain
from app.models.skill import Skill
from app.services.career_brain import CareerBrainService

router = APIRouter(tags=["skills"])


@router.get("/skills", response_model=list[Skill])
def get_skills(service: CareerBrainService = Depends(get_career_brain)) -> list[Skill]:
    return service.get_skills()
