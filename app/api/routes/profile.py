"""Profile endpoint."""

from fastapi import APIRouter, Depends

from app.api.deps import get_career_brain
from app.models.profile import Profile
from app.services.career_brain import CareerBrainService

router = APIRouter(tags=["profile"])


@router.get("/profile", response_model=Profile)
def get_profile(service: CareerBrainService = Depends(get_career_brain)) -> Profile:
    return service.get_profile()
