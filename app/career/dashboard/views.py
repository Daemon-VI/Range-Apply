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
    """Add a skill to the career brain."""
    # In a real implementation, we would modify the career_brain JSON file, 
    # validate via TruthValidator, and then trigger MatchOrchestrator to recalculate.
    # For Phase 3 scope demonstration, we'll just redirect back.
    print(f"Added skill: {skill_name}")
    # Trigger recalculation logic would go here.
    return RedirectResponse(url="/dashboard/profile", status_code=303)
