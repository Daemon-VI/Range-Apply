"""Experience endpoint."""

from fastapi import APIRouter, Depends

from app.api.deps import get_career_brain
from app.models.experience import Experience
from app.services.career_brain import CareerBrainService

router = APIRouter(tags=["experience"])


@router.get("/experience", response_model=list[Experience])
def get_experience(service: CareerBrainService = Depends(get_career_brain)) -> list[Experience]:
    return service.get_experience()
