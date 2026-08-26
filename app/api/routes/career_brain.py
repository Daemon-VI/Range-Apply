"""Career Brain summary endpoint."""

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_career_brain
from app.models.career_fact import CareerFact
from app.models.project import Project
from app.services.career_brain import CareerBrainService, CareerSummary

router = APIRouter(tags=["career-brain"])


@router.get("/career-brain", response_model=CareerSummary)
def get_career_brain_summary(
    service: CareerBrainService = Depends(get_career_brain),
) -> CareerSummary:
    return service.get_career_summary()


@router.get("/career-brain/facts/verified", response_model=list[CareerFact])
def get_verified_facts(
    service: CareerBrainService = Depends(get_career_brain),
) -> list[CareerFact]:
    return service.get_verified_facts()


@router.get("/career-brain/facts/application-safe", response_model=list[CareerFact])
def get_application_safe_facts(
    service: CareerBrainService = Depends(get_career_brain),
) -> list[CareerFact]:
    return service.get_application_safe_facts()


@router.get("/career-brain/projects/search", response_model=list[Project])
def search_projects(
    q: str = Query(..., min_length=1),
    service: CareerBrainService = Depends(get_career_brain),
) -> list[Project]:
    return service.search_projects(q)


@router.get("/career-brain/projects/relevant", response_model=list[Project])
def find_relevant_projects(
    role: str = Query(..., min_length=1),
    service: CareerBrainService = Depends(get_career_brain),
) -> list[Project]:
    return service.find_relevant_projects(role)
