import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.services.career_brain import CareerBrainService

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
# Fallback to the main base template path if we want to share base.html
# For simplicity, we assume templates can find base.html if we point them to the same root,
# but Jinja allows multiple directories.
import app.jobs.dashboard.views as jobs_views
templates = Jinja2Templates(directory=[str(TEMPLATES_DIR), str(jobs_views.TEMPLATES_DIR)])

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard/profile", tags=["dashboard", "profile"])

# Dummy dependency for now. In a real app, this would be injected properly.
def get_career_brain() -> CareerBrainService:
    service = CareerBrainService()
    service.load()
    return service

@router.get("/", response_class=HTMLResponse)
def view_profile(request: Request, career_brain: CareerBrainService = Depends(get_career_brain)):
    """View Career Brain profile and edit forms."""
    summary = career_brain.get_career_summary()
    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={
            "summary": summary,
            "skills": career_brain.get_skills(),
            "projects": career_brain.get_projects(),
        },
    )

@router.post("/skill")
def add_skill(
    request: Request, 
    skill_name: str = Form(...),
    career_brain: CareerBrainService = Depends(get_career_brain)
):
    """Accept a new skill for the career brain.

    NOT YET PERSISTED. Writing to the Career Brain requires the versioned
    profile state and TruthValidator gate described in PRD P1, which is not
    implemented; silently pretending the write succeeded would be worse than
    saying so. Tracked as an open item in docs/PROJECT_STATE.md.
    """
    logger.warning(
        "Skill submission '%s' was not persisted: Career Brain writes are not implemented yet.",
        skill_name,
    )
    return RedirectResponse(url="/dashboard/profile?notice=not-persisted", status_code=303)
