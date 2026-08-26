"""Projects endpoints."""

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_career_brain
from app.models.project import Project
from app.services.career_brain import CareerBrainService

router = APIRouter(tags=["projects"])


@router.get("/projects", response_model=list[Project])
def get_projects(service: CareerBrainService = Depends(get_career_brain)) -> list[Project]:
    return service.get_projects()


@router.get("/projects/{project_id}", response_model=Project)
def get_project(
    project_id: str,
    service: CareerBrainService = Depends(get_career_brain),
) -> Project:
    try:
        return service.get_project(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Project not found: {project_id}") from None
