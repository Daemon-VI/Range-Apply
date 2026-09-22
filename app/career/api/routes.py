"""Career Brain write API: evidence, profile, positioning variants, answer bank.

Reads are open (single-user product, same as the other read endpoints);
every write requires ``X-API-Key``. Errors are ``CareerOSError`` subclasses
raised by the repository and mapped by ``app/main.py``.
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from app.api.deps import get_repository
from app.career.database.models import EvidenceRelationshipRow
from app.career.importer import SeedImporter
from app.career.models import (
    AnswerBankEntry,
    AnswerBankEntryCreate,
    AnswerBankEntryUpdate,
    AnswerStatus,
    AuditEvent,
    EvidenceKind,
    EvidenceNode,
    EvidenceNodeCreate,
    EvidenceNodeUpdate,
    EvidenceRelationship,
    ImportReport,
    PositioningVariant,
    PositioningVariantCreate,
    PositioningVariantUpdate,
    RelationType,
)
from app.career.read_model import preferences_from_row, profile_from_row
from app.career.repository import EvidenceRepository
from app.config import settings
from app.core.errors import NotFoundError, ValidationFailed
from app.models.preference import Preference
from app.models.profile import Profile
from app.security import require_api_key

router = APIRouter(prefix="/api/v1/career", tags=["career-brain"])

WRITE = [Depends(require_api_key)]
API_ACTOR = "api"


def _relationship(row: EvidenceRelationshipRow) -> EvidenceRelationship:
    return EvidenceRelationship(
        id=row.id,
        tenant_id=row.tenant_id,
        from_key=row.from_node.key,
        to_key=row.to_node.key,
        relation=RelationType(row.relation),
        position=row.position,
        created_at=row.created_at,
    )


def _variant(row) -> PositioningVariant:
    data = EvidenceRepository.variant_snapshot(row)
    return PositioningVariant(
        id=row.id,
        tenant_id=row.tenant_id,
        role_family=row.role_family,
        name=row.name,
        headline=row.headline,
        summary=row.summary,
        evidence=data["evidence"],
        is_active=row.is_active,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ---------------------------------------------------------------------- #
# profile & preferences
# ---------------------------------------------------------------------- #


class ProfileUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1)
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    work_authorization: Optional[str] = None
    degree: Optional[str] = None
    branch: Optional[str] = None
    college: Optional[str] = None
    graduation_year: Optional[int] = None
    current_academic_status: Optional[str] = None
    cgpa: Optional[float] = None
    backlogs: Optional[str] = None
    github: Optional[str] = None
    linkedin: Optional[str] = None
    portfolio: Optional[str] = None
    positioning_statement: Optional[str] = None
    long_term_goal: Optional[str] = None


def _require_profile(repo: EvidenceRepository):
    row = repo.get_profile_row()
    if row is None:
        raise NotFoundError(f"No candidate profile for tenant {repo.tenant_id}")
    return row


@router.get("/profile", response_model=Profile)
def get_profile(repo: EvidenceRepository = Depends(get_repository)) -> Profile:
    return profile_from_row(_require_profile(repo))


@router.put("/profile", response_model=Profile, dependencies=WRITE)
def update_profile(
    body: ProfileUpdate, repo: EvidenceRepository = Depends(get_repository)
) -> Profile:
    _require_profile(repo)
    fields = body.model_dump(exclude_unset=True)
    row, _ = repo.upsert_profile(fields, None, actor=API_ACTOR)
    repo.commit()
    return profile_from_row(row)


@router.get("/preferences", response_model=Preference)
def get_preferences(repo: EvidenceRepository = Depends(get_repository)) -> Preference:
    return preferences_from_row(_require_profile(repo))


@router.put("/preferences", response_model=Preference, dependencies=WRITE)
def update_preferences(
    body: Preference, repo: EvidenceRepository = Depends(get_repository)
) -> Preference:
    current = _require_profile(repo)
    # Audit fix (2026-09-14): the body replaced every preference, so a client
    # sending only the fields it edits reset the rest to model defaults (target
    # roles wiped, a switched-off "include unconfirmed locations" switched back
    # on). Only the fields present in the request change.
    merged = preferences_from_row(current).model_dump()
    merged.update(body.model_dump(exclude_unset=True))
    row, _ = repo.upsert_profile({}, Preference(**merged).model_dump(), actor=API_ACTOR)
    repo.commit()
    return preferences_from_row(row)


# ---------------------------------------------------------------------- #
# evidence nodes
# ---------------------------------------------------------------------- #


@router.get("/evidence", response_model=list[EvidenceNode])
def list_evidence(
    kind: Optional[EvidenceKind] = Query(default=None),
    include_removed: bool = Query(default=False),
    repo: EvidenceRepository = Depends(get_repository),
) -> list[EvidenceNode]:
    return [EvidenceNode.model_validate(r) for r in repo.list_nodes(kind, include_removed)]


@router.post(
    "/evidence",
    response_model=EvidenceNode,
    status_code=status.HTTP_201_CREATED,
    dependencies=WRITE,
)
def create_evidence(
    body: EvidenceNodeCreate, repo: EvidenceRepository = Depends(get_repository)
) -> EvidenceNode:
    row = repo.create_node(body, actor=API_ACTOR)
    repo.commit()
    return EvidenceNode.model_validate(row)


@router.get("/evidence/{key}", response_model=EvidenceNode)
def get_evidence(key: str, repo: EvidenceRepository = Depends(get_repository)) -> EvidenceNode:
    return EvidenceNode.model_validate(repo.require_node(key))


@router.patch("/evidence/{key}", response_model=EvidenceNode, dependencies=WRITE)
def update_evidence(
    key: str, body: EvidenceNodeUpdate, repo: EvidenceRepository = Depends(get_repository)
) -> EvidenceNode:
    row = repo.update_node(key, body, actor=API_ACTOR)
    repo.commit()
    return EvidenceNode.model_validate(row)


@router.delete("/evidence/{key}", response_model=EvidenceNode, dependencies=WRITE)
def remove_evidence(
    key: str,
    reason: Optional[str] = Query(default=None),
    repo: EvidenceRepository = Depends(get_repository),
) -> EvidenceNode:
    """Soft removal: the node stays for audit and old references, graded REMOVED."""
    row = repo.remove_node(key, actor=API_ACTOR, reason=reason)
    repo.commit()
    return EvidenceNode.model_validate(row)


@router.post("/evidence/{key}/restore", response_model=EvidenceNode, dependencies=WRITE)
def restore_evidence(key: str, repo: EvidenceRepository = Depends(get_repository)) -> EvidenceNode:
    row = repo.restore_node(key, actor=API_ACTOR)
    repo.commit()
    return EvidenceNode.model_validate(row)


@router.get("/evidence/{key}/history", response_model=list[AuditEvent])
def evidence_history(
    key: str, repo: EvidenceRepository = Depends(get_repository)
) -> list[AuditEvent]:
    repo.require_node(key)
    return [AuditEvent.model_validate(e) for e in repo.list_audit("evidence_node", key)]


@router.get("/evidence/{key}/relationships", response_model=list[EvidenceRelationship])
def evidence_relationships(
    key: str, repo: EvidenceRepository = Depends(get_repository)
) -> list[EvidenceRelationship]:
    return [_relationship(r) for r in repo.list_relationships(key)]


# ---------------------------------------------------------------------- #
# relationships
# ---------------------------------------------------------------------- #


class RelationshipRequest(BaseModel):
    from_key: str
    to_key: str
    relation: RelationType
    position: int = 0


@router.get("/relationships", response_model=list[EvidenceRelationship])
def list_relationships(repo: EvidenceRepository = Depends(get_repository)) -> list[EvidenceRelationship]:
    return [_relationship(r) for r in repo.list_relationships()]


@router.post(
    "/relationships",
    response_model=EvidenceRelationship,
    status_code=status.HTTP_201_CREATED,
    dependencies=WRITE,
)
def create_relationship(
    body: RelationshipRequest, repo: EvidenceRepository = Depends(get_repository)
) -> EvidenceRelationship:
    row, _ = repo.add_relationship(body.from_key, body.to_key, body.relation, API_ACTOR, body.position)
    repo.commit()
    return _relationship(row)


@router.delete("/relationships", dependencies=WRITE)
def delete_relationship(
    body: RelationshipRequest, repo: EvidenceRepository = Depends(get_repository)
) -> dict[str, Any]:
    deleted = repo.remove_relationship(body.from_key, body.to_key, body.relation, API_ACTOR)
    repo.commit()
    return {"deleted": deleted}


# ---------------------------------------------------------------------- #
# positioning variants
# ---------------------------------------------------------------------- #


@router.get("/positioning", response_model=list[PositioningVariant])
def list_positioning(
    role_family: Optional[str] = Query(default=None),
    active_only: bool = Query(default=False),
    repo: EvidenceRepository = Depends(get_repository),
) -> list[PositioningVariant]:
    return [_variant(r) for r in repo.list_variants(role_family, active_only)]


@router.post(
    "/positioning",
    response_model=PositioningVariant,
    status_code=status.HTTP_201_CREATED,
    dependencies=WRITE,
)
def create_positioning(
    body: PositioningVariantCreate, repo: EvidenceRepository = Depends(get_repository)
) -> PositioningVariant:
    row = repo.create_variant(body, actor=API_ACTOR)
    repo.commit()
    return _variant(row)


@router.get("/positioning/{variant_id}", response_model=PositioningVariant)
def get_positioning(
    variant_id: str, repo: EvidenceRepository = Depends(get_repository)
) -> PositioningVariant:
    return _variant(repo.require_variant(variant_id))


@router.patch("/positioning/{variant_id}", response_model=PositioningVariant, dependencies=WRITE)
def update_positioning(
    variant_id: str,
    body: PositioningVariantUpdate,
    repo: EvidenceRepository = Depends(get_repository),
) -> PositioningVariant:
    row = repo.update_variant(variant_id, body, actor=API_ACTOR)
    repo.commit()
    return _variant(row)


@router.delete("/positioning/{variant_id}", dependencies=WRITE)
def delete_positioning(
    variant_id: str, repo: EvidenceRepository = Depends(get_repository)
) -> dict[str, Any]:
    repo.delete_variant(variant_id, actor=API_ACTOR)
    repo.commit()
    return {"deleted": True}


# ---------------------------------------------------------------------- #
# answer bank
# ---------------------------------------------------------------------- #


@router.get("/answers", response_model=list[AnswerBankEntry])
def list_answers(
    category: Optional[str] = Query(default=None),
    status_filter: Optional[AnswerStatus] = Query(default=None, alias="status"),
    repo: EvidenceRepository = Depends(get_repository),
) -> list[AnswerBankEntry]:
    return [AnswerBankEntry.model_validate(r) for r in repo.list_answers(category, status_filter)]


@router.get("/answers/lookup", response_model=Optional[AnswerBankEntry])
def lookup_answer(
    question: str = Query(..., min_length=1),
    repo: EvidenceRepository = Depends(get_repository),
) -> Optional[AnswerBankEntry]:
    """Deterministic reuse: the approved answer for this exact question, or null."""
    row = repo.find_answer(question)
    return AnswerBankEntry.model_validate(row) if row is not None else None


@router.post(
    "/answers",
    response_model=AnswerBankEntry,
    status_code=status.HTTP_201_CREATED,
    dependencies=WRITE,
)
def create_answer(
    body: AnswerBankEntryCreate, repo: EvidenceRepository = Depends(get_repository)
) -> AnswerBankEntry:
    row = repo.create_answer(body, actor=API_ACTOR)
    repo.commit()
    return AnswerBankEntry.model_validate(row)


@router.get("/answers/{entry_id}", response_model=AnswerBankEntry)
def get_answer(entry_id: str, repo: EvidenceRepository = Depends(get_repository)) -> AnswerBankEntry:
    return AnswerBankEntry.model_validate(repo.require_answer(entry_id))


@router.patch("/answers/{entry_id}", response_model=AnswerBankEntry, dependencies=WRITE)
def update_answer(
    entry_id: str, body: AnswerBankEntryUpdate, repo: EvidenceRepository = Depends(get_repository)
) -> AnswerBankEntry:
    row = repo.update_answer(entry_id, body, actor=API_ACTOR)
    repo.commit()
    return AnswerBankEntry.model_validate(row)


@router.post("/answers/{entry_id}/approve", response_model=AnswerBankEntry, dependencies=WRITE)
def approve_answer(entry_id: str, repo: EvidenceRepository = Depends(get_repository)) -> AnswerBankEntry:
    row = repo.approve_answer(entry_id, actor=API_ACTOR)
    repo.commit()
    return AnswerBankEntry.model_validate(row)


@router.delete("/answers/{entry_id}", dependencies=WRITE)
def delete_answer(entry_id: str, repo: EvidenceRepository = Depends(get_repository)) -> dict[str, Any]:
    repo.delete_answer(entry_id, actor=API_ACTOR)
    repo.commit()
    return {"deleted": True}


# ---------------------------------------------------------------------- #
# import & audit
# ---------------------------------------------------------------------- #


class ImportRequest(BaseModel):
    file: Optional[str] = Field(default=None, description="Defaults to CAREER_DATA_PATH")
    force: bool = False


@router.post("/import", response_model=ImportReport, dependencies=WRITE)
def import_seed(
    body: Optional[ImportRequest] = None, repo: EvidenceRepository = Depends(get_repository)
) -> ImportReport:
    body = body or ImportRequest()
    path = body.file or settings.career_data_path
    # Audit fix (2026-09-14): a missing or non-JSON file was an unhandled 500.
    try:
        return SeedImporter(repo).import_file(path, force=body.force)
    except FileNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    except (ValueError, UnicodeDecodeError) as exc:  # json.JSONDecodeError is a ValueError
        raise ValidationFailed(f"Career data file is not valid JSON: {type(exc).__name__}") from exc


@router.get("/audit", response_model=list[AuditEvent])
def list_audit(
    entity_type: Optional[str] = Query(default=None),
    entity_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    repo: EvidenceRepository = Depends(get_repository),
) -> list[AuditEvent]:
    return [AuditEvent.model_validate(e) for e in repo.list_audit(entity_type, entity_id, limit)]
