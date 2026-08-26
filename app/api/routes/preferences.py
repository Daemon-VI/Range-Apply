"""Preferences endpoint."""

from fastapi import APIRouter, Depends

from app.api.deps import get_career_brain
from app.models.preference import Preference
from app.services.career_brain import CareerBrainService

router = APIRouter(tags=["preferences"])


@router.get("/preferences", response_model=Preference)
def get_preferences(service: CareerBrainService = Depends(get_career_brain)) -> Preference:
    return service.get_preferences()
