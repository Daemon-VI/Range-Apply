"""Career Brain dashboard: view and edit evidence, positioning, answers.

Server-rendered Jinja2 with plain HTML forms (POST -> redirect), enhanced by
htmx ``hx-boost`` in ``base.html`` so navigation stays in place. Every write
goes through ``EvidenceRepository`` with actor ``"dashboard"`` so it is
audited exactly like an API write.
"""

import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import app.jobs.dashboard.views as jobs_views
from app.api.deps import get_career_brain, get_repository
from app.career.models import (
    AnswerBankEntryCreate,
    AnswerStatus,
    EvidenceGrade,
    EvidenceKind,
    EvidenceNodeCreate,
    EvidenceNodeUpdate,
    EvidenceSourceType,
    PositioningVariantCreate,
    PositioningVariantEvidence,
    grade_for,
)
from app.career.read_model import preferences_from_row
from app.career.repository import EvidenceRepository
from app.core.errors import CareerOSError, ValidationFailed
from app.jobs.geography import parse_target, policy_from_preferences
from app.models.enums import VerificationStatus
from app.services.career_brain import CareerBrainService

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
# The profile templates extend the jobs dashboard's base.html, so both
# directories are on the Jinja search path.
templates = Jinja2Templates(directory=[str(TEMPLATES_DIR), str(jobs_views.TEMPLATES_DIR)])

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard/profile", tags=["dashboard", "profile"])

ACTOR = "dashboard"
BASE = "/dashboard/profile/"

KIND_ORDER = [
    EvidenceKind.SKILL,
    EvidenceKind.PROJECT,
    EvidenceKind.EXPERIENCE,
    EvidenceKind.EDUCATION,
    EvidenceKind.ACHIEVEMENT,
    EvidenceKind.METRIC,
    EvidenceKind.RESPONSIBILITY,
    EvidenceKind.FACT,
    EvidenceKind.LINK,
    EvidenceKind.CREDENTIAL,
    EvidenceKind.OTHER,
]

GRADE_BADGE = {
    EvidenceGrade.CONFIRMED: "badge-green",
    EvidenceGrade.APPROXIMATE: "badge-blue",
    EvidenceGrade.UNVERIFIED: "badge-orange",
    EvidenceGrade.NEEDS_REVIEW: "badge-purple",
    EvidenceGrade.REMOVED: "badge-muted",
}


def _redirect(notice: str) -> RedirectResponse:
    return RedirectResponse(url=f"{BASE}?notice={notice}", status_code=303)


def _handle(action):
    """Run a repository write; render structured errors as a notice, not a 500."""
    try:
        action()
    except CareerOSError as exc:
        logger.info("Dashboard write refused: %s", exc.code)
        return _redirect(f"error:{exc.code}:{exc.message[:120]}")
    return None


@router.get("/", response_class=HTMLResponse)
def view_profile(
    request: Request,
    notice: Optional[str] = None,
    show_removed: bool = False,
    career_brain: CareerBrainService = Depends(get_career_brain),
    repo: EvidenceRepository = Depends(get_repository),
):
    """Career Brain: profile, evidence by kind, positioning variants, answer bank."""
    summary = career_brain.get_career_summary()
    nodes = repo.list_nodes(include_removed=show_removed)
    grouped: dict[str, list] = {}
    for node in nodes:
        grouped.setdefault(node.kind, []).append(node)
    sections = [(kind.value, grouped[kind.value]) for kind in KIND_ORDER if kind.value in grouped]
    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={
            "summary": summary,
            "profile": career_brain.get_profile(),
            "preferences": career_brain.get_preferences(),
            "geography": policy_from_preferences(career_brain.get_preferences(), career_brain.get_profile().location),
            "skills": career_brain.get_skills(),
            "projects": career_brain.get_projects(),
            "sections": sections,
            "variants": repo.list_variants(),
            "answers": repo.list_answers(),
            "audit": repo.list_audit(limit=25),
            "notice": notice,
            "show_removed": show_removed,
            "kinds": [k.value for k in EvidenceKind],
            "statuses": [s.value for s in VerificationStatus],
            "source_types": [s.value for s in EvidenceSourceType],
            "answer_statuses": [s.value for s in AnswerStatus],
            "grade_for": lambda node: grade_for(
                VerificationStatus(node.verification_status), node.status == "REMOVED"
            ),
            "grade_badge": lambda grade: GRADE_BADGE.get(grade, "badge-blue"),
        },
    )


# ---------------------------------------------------------------------- #
# profile
# ---------------------------------------------------------------------- #


@router.post("/details")
def update_details(
    email: str = Form(""),
    phone: str = Form(""),
    location: str = Form(""),
    work_authorization: str = Form(""),
    github: str = Form(""),
    linkedin: str = Form(""),
    portfolio: str = Form(""),
    positioning_statement: str = Form(""),
    repo: EvidenceRepository = Depends(get_repository),
):
    fields = {
        "email": email.strip() or None,
        "phone": phone.strip() or None,
        "location": location.strip() or None,
        "work_authorization": work_authorization.strip() or None,
        "github": github.strip() or None,
        "linkedin": linkedin.strip() or None,
        "portfolio": portfolio.strip() or None,
        "positioning_statement": positioning_statement.strip(),
    }

    def action():
        repo.upsert_profile(fields, None, actor=ACTOR)
        repo.commit()

    return _handle(action) or _redirect("profile-saved")


@router.post("/location-preference")
def update_location_preference(
    location_primary: str = Form(""),
    also_consider: str = Form(""),
    include_country_remote: Optional[str] = Form(None),
    include_other_cities: Optional[str] = Form(None),
    allow_international: Optional[str] = Form(None),
    include_unconfirmed: Optional[str] = Form(None),
    repo: EvidenceRepository = Depends(get_repository),
):
    """Job location preference: where discovery, eligibility and admission look (no code change to retarget)."""

    def action():
        row = repo.get_profile_row()
        if row is None:
            raise ValidationFailed("Import or create a candidate profile before setting a location preference.")
        preferences = preferences_from_row(row).model_dump()
        preferences.update(
            location_primary=location_primary.strip() or None,
            # One entry per comma or line (audit 2026-09-14: "Pune\nRemote" was stored as one location).
            preferred_locations=[part.strip() for part in re.split(r"[,\n;]", also_consider) if part.strip()],
            location_include_country_remote=include_country_remote is not None,
            location_include_other_cities=include_other_cities is not None,
            location_allow_international=allow_international is not None,
            location_include_unconfirmed=include_unconfirmed is not None,
        )
        repo.upsert_profile({}, preferences, actor=ACTOR)
        repo.commit()

    target = parse_target(location_primary.strip()) if location_primary.strip() else None
    if target is not None and target.country is None:
        # Saved as typed, but say so: an unrecognised city is matched by its name only (audit 2026-09-14).
        return _handle(action) or _redirect("location-preference-saved-city-not-recognised-add-state-and-country")
    return _handle(action) or _redirect("location-preference-saved")


def _role_list(text: str) -> list[str]:
    """One role per comma, semicolon or line; blanks and repeats dropped, order kept."""
    seen: dict[str, str] = {}
    for part in re.split(r"[,\n;]", text or ""):
        role = part.strip()
        if role and role.lower() not in seen:
            seen[role.lower()] = role[:120]
    return list(seen.values())


@router.post("/target-roles")
def update_target_roles(
    tier1: str = Form(""),
    tier2: str = Form(""),
    lower_priority: str = Form(""),
    include_unrelated: Optional[str] = Form(None),
    repo: EvidenceRepository = Depends(get_repository),
):
    """The existing target-role preferences (tier 1 / tier 2 / lower priority), edited in place.

    Saved on the preferences only: existing matches keep their stored relevance
    until matching is re-run (Desktop → Opportunities → Re-run matching).
    """

    def action():
        row = repo.get_profile_row()
        if row is None:
            raise ValidationFailed("Import or create a candidate profile before setting target roles.")
        preferences = preferences_from_row(row).model_dump()
        preferences.update(
            target_roles_tier1=_role_list(tier1),
            target_roles_tier2=_role_list(tier2),
            target_roles_lower_priority=_role_list(lower_priority),
            role_include_unrelated=include_unrelated is not None,
        )
        repo.upsert_profile({}, preferences, actor=ACTOR)
        repo.commit()

    return _handle(action) or _redirect("target-roles-saved-re-run-matching-to-apply-them-to-existing-opportunities")


# ---------------------------------------------------------------------- #
# evidence
# ---------------------------------------------------------------------- #


@router.post("/evidence")
def add_evidence(
    kind: str = Form(...),
    label: str = Form(...),
    claim: str = Form(""),
    verification_status: str = Form(VerificationStatus.UNVERIFIED.value),
    source_type: str = Form(EvidenceSourceType.CANDIDATE_ENTERED.value),
    source_ref: str = Form(""),
    repo: EvidenceRepository = Depends(get_repository),
):
    def action():
        repo.create_node(
            EvidenceNodeCreate(
                kind=EvidenceKind(kind),
                label=label.strip(),
                claim=claim.strip() or label.strip(),
                verification_status=VerificationStatus(verification_status),
                source_type=EvidenceSourceType(source_type),
                source_ref=source_ref.strip() or None,
            ),
            actor=ACTOR,
        )
        repo.commit()

    return _handle(action) or _redirect("evidence-added")


@router.post("/skill")
def add_skill(
    skill_name: str = Form(...),
    repo: EvidenceRepository = Depends(get_repository),
):
    """Legacy quick-add form: a candidate-entered skill, UNVERIFIED until confirmed."""

    def action():
        repo.create_node(
            EvidenceNodeCreate(
                kind=EvidenceKind.SKILL,
                label=skill_name.strip(),
                claim=skill_name.strip(),
                attributes={"category": "Other"},
                verification_status=VerificationStatus.UNVERIFIED,
                source_type=EvidenceSourceType.CANDIDATE_ENTERED,
            ),
            actor=ACTOR,
        )
        repo.commit()

    return _handle(action) or _redirect("skill-added")


@router.post("/evidence/{node_key}/status")
def set_evidence_status(
    node_key: str,
    verification_status: str = Form(...),
    repo: EvidenceRepository = Depends(get_repository),
):
    def action():
        repo.update_node(node_key, EvidenceNodeUpdate(verification_status=VerificationStatus(verification_status)),
            actor=ACTOR,
        )
        repo.commit()

    return _handle(action) or _redirect("status-updated")


@router.post("/evidence/{node_key}/edit")
def edit_evidence(
    node_key: str,
    label: str = Form(...),
    claim: str = Form(...),
    repo: EvidenceRepository = Depends(get_repository),
):
    def action():
        repo.update_node(node_key, EvidenceNodeUpdate(label=label.strip(), claim=claim.strip()), actor=ACTOR
        )
        repo.commit()

    return _handle(action) or _redirect("evidence-updated")


@router.post("/evidence/{node_key}/remove")
def remove_evidence(
    node_key: str, reason: str = Form(""), repo: EvidenceRepository = Depends(get_repository)
):
    def action():
        repo.remove_node(node_key, actor=ACTOR, reason=reason.strip() or None)
        repo.commit()

    return _handle(action) or _redirect("evidence-removed")


@router.post("/evidence/{node_key}/restore")
def restore_evidence(node_key: str, repo: EvidenceRepository = Depends(get_repository)):
    def action():
        repo.restore_node(node_key, actor=ACTOR)
        repo.commit()

    return _handle(action) or _redirect("evidence-restored")


# ---------------------------------------------------------------------- #
# positioning variants
# ---------------------------------------------------------------------- #


@router.post("/positioning")
def add_positioning(
    role_family: str = Form(...),
    headline: str = Form(""),
    summary: str = Form(""),
    evidence_keys: str = Form(""),
    repo: EvidenceRepository = Depends(get_repository),
):
    keys = [k.strip() for k in evidence_keys.replace("\n", ",").split(",") if k.strip()]

    def action():
        repo.create_variant(
            PositioningVariantCreate(
                role_family=role_family.strip(),
                headline=headline.strip(),
                summary=summary.strip(),
                evidence=[PositioningVariantEvidence(key=k, position=i) for i, k in enumerate(keys)],
            ),
            actor=ACTOR,
        )
        repo.commit()

    return _handle(action) or _redirect("variant-added")


@router.post("/positioning/{variant_id}/delete")
def delete_positioning(variant_id: str, repo: EvidenceRepository = Depends(get_repository)):
    def action():
        repo.delete_variant(variant_id, actor=ACTOR)
        repo.commit()

    return _handle(action) or _redirect("variant-deleted")


# ---------------------------------------------------------------------- #
# answer bank
# ---------------------------------------------------------------------- #


@router.post("/answers")
def add_answer(
    category: str = Form(...),
    question: str = Form(...),
    answer: str = Form(...),
    evidence_keys: str = Form(""),
    repo: EvidenceRepository = Depends(get_repository),
):
    keys = [k.strip() for k in evidence_keys.replace("\n", ",").split(",") if k.strip()]

    def action():
        repo.create_answer(
            AnswerBankEntryCreate(
                category=category.strip(), question=question.strip(), answer=answer.strip(),
                evidence_keys=keys,
            ),
            actor=ACTOR,
        )
        repo.commit()

    return _handle(action) or _redirect("answer-added")


@router.post("/answers/{entry_id}/approve")
def approve_answer(entry_id: str, repo: EvidenceRepository = Depends(get_repository)):
    def action():
        repo.approve_answer(entry_id, actor=ACTOR)
        repo.commit()

    return _handle(action) or _redirect("answer-approved")


@router.post("/answers/{entry_id}/delete")
def delete_answer(entry_id: str, repo: EvidenceRepository = Depends(get_repository)):
    def action():
        repo.delete_answer(entry_id, actor=ACTOR)
        repo.commit()

    return _handle(action) or _redirect("answer-deleted")
