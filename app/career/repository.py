"""Tenant-scoped persistence for the Evidence Graph.

Every method is bound to one ``tenant_id`` at construction, so no query in
this module can cross tenants by omission. All mutations bump ``version`` and
write a before/after :class:`AuditEventRow`; nothing important changes
silently. Callers own the transaction boundary (``commit()``), mirroring how
``JobDeduplicator`` works with ``commit=False`` inside the pipeline.

Truth rules enforced here (mirroring ``TruthValidator``):

* an ``INFERRED`` or ``LLM_EXTRACTED`` claim can never be *created* as
  VERIFIED — extraction is not verification; a human confirms it later, and
  that confirmation is recorded in ``verified_by``/``verified_at``;
* ``allowed_for_application`` requires VERIFIED; CONFLICT/INFERRED evidence
  can be neither resume- nor application-safe.
"""

import logging
import re
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.career.database.models import (
    AnswerBankEntryRow,
    AuditEventRow,
    CandidateProfileRow,
    EvidenceNodeRow,
    EvidenceRelationshipRow,
    PositioningVariantEvidenceRow,
    PositioningVariantRow,
    TenantRow,
)
from app.career.models import (
    UNTRUSTED_SOURCE_TYPES,
    AnswerBankEntryCreate,
    AnswerBankEntryUpdate,
    AnswerStatus,
    EvidenceKind,
    EvidenceNodeCreate,
    EvidenceNodeUpdate,
    EvidenceSourceType,
    EvidenceStatus,
    PositioningVariantCreate,
    PositioningVariantEvidence,
    PositioningVariantUpdate,
    RelationType,
)
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.core.timeutils import db_now, ordered_db_now
from app.models.enums import VerificationStatus

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_length: int = 60) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:max_length].strip("-") or "item"


def normalize_question(question: str) -> str:
    """Deterministic lookup key for an application question."""
    text = question.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:512]


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def check_truth_rules(
    status: VerificationStatus, allowed_for_resume: bool, allowed_for_application: bool
) -> None:
    """Raise ``ValidationFailed`` when the allowed flags overstate the status."""
    if status is VerificationStatus.CONFLICT and (allowed_for_resume or allowed_for_application):
        raise ValidationFailed("Conflicting evidence cannot be resume- or application-safe.")
    if status is VerificationStatus.INFERRED and (allowed_for_resume or allowed_for_application):
        raise ValidationFailed("Inferred evidence cannot be promoted to safe without verification.")
    if status is not VerificationStatus.VERIFIED and allowed_for_application:
        raise ValidationFailed("Only VERIFIED evidence can be application-safe.")


class EvidenceRepository:
    """All reads and writes for one tenant's Career Brain."""

    def __init__(self, db: Session, tenant_id: str):
        if not tenant_id:
            raise ValidationFailed("tenant_id is required")
        self.db = db
        self.tenant_id = tenant_id

    # ------------------------------------------------------------------ #
    # transaction helpers
    # ------------------------------------------------------------------ #

    def commit(self) -> None:
        self.db.commit()

    def flush(self) -> None:
        self.db.flush()

    # ------------------------------------------------------------------ #
    # tenants
    # ------------------------------------------------------------------ #

    def ensure_tenant(self, name: str = "") -> TenantRow:
        row = self.db.get(TenantRow, self.tenant_id)
        if row is None:
            row = TenantRow(id=self.tenant_id, name=name or self.tenant_id)
            self.db.add(row)
            self.db.flush()
        return row

    # ------------------------------------------------------------------ #
    # audit
    # ------------------------------------------------------------------ #

    def record(
        self,
        entity_type: str,
        entity_id: str,
        action: str,
        actor: str,
        before: Optional[dict[str, Any]] = None,
        after: Optional[dict[str, Any]] = None,
        summary: Optional[str] = None,
    ) -> AuditEventRow:
        event = AuditEventRow(
            tenant_id=self.tenant_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            actor=actor or "system",
            before=before,
            after=after,
            summary=(summary or "")[:512] or None,
            created_at=ordered_db_now(),
        )
        self.db.add(event)
        self.db.flush()
        return event

    def list_audit(
        self,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[AuditEventRow]:
        query = self.db.query(AuditEventRow).filter(AuditEventRow.tenant_id == self.tenant_id)
        if entity_type:
            query = query.filter(AuditEventRow.entity_type == entity_type)
        if entity_id:
            query = query.filter(AuditEventRow.entity_id == entity_id)
        return (
            query.order_by(AuditEventRow.created_at.desc(), AuditEventRow.id.desc())
            .limit(limit)
            .all()
        )

    # ------------------------------------------------------------------ #
    # profile
    # ------------------------------------------------------------------ #

    PROFILE_FIELDS = (
        "name",
        "email",
        "phone",
        "location",
        "work_authorization",
        "degree",
        "branch",
        "college",
        "graduation_year",
        "current_academic_status",
        "cgpa",
        "backlogs",
        "github",
        "linkedin",
        "portfolio",
        "positioning_statement",
        "long_term_goal",
    )

    #: Profile links are filled into application forms and rendered as links.
    PROFILE_LINK_FIELDS = ("github", "linkedin", "portfolio")

    @classmethod
    def _check_profile_values(cls, fields: dict[str, Any]) -> None:
        """Refuse values no real profile can hold.

        Audit fix (2026-09-14): graduation year 3027, CGPA 42 and a
        ``javascript:`` LinkedIn URL were stored as-is from both the API and
        the dashboard; the year drives the eligibility gates and the links are
        rendered and filled into employer forms.
        """
        year = fields.get("graduation_year")
        if year is not None and (not isinstance(year, int) or isinstance(year, bool) or not 1950 <= year <= 2100):
            raise ValidationFailed(f"graduation_year must be a year between 1950 and 2100, got {year!r}.")
        cgpa = fields.get("cgpa")
        if cgpa is not None and (isinstance(cgpa, bool) or not isinstance(cgpa, (int, float)) or not 0 <= cgpa <= 100):
            raise ValidationFailed(f"cgpa must be a number between 0 and 100, got {cgpa!r}.")
        for name in cls.PROFILE_LINK_FIELDS:
            value = fields.get(name)
            if not value:
                continue
            scheme = re.match(r"^\s*([a-zA-Z][a-zA-Z0-9+.-]*):", str(value))
            if scheme and scheme.group(1).lower() not in ("http", "https") and not re.match(r"^\s*[\w.-]+\.[a-z]{2,}:\d", str(value), re.IGNORECASE):
                raise ValidationFailed(f"{name} must be an http(s) link, not a {scheme.group(1).lower()}: URL.")

    def get_profile_row(self) -> Optional[CandidateProfileRow]:
        return (
            self.db.query(CandidateProfileRow)
            .filter(CandidateProfileRow.tenant_id == self.tenant_id)
            .first()
        )

    @classmethod
    def profile_snapshot(cls, row: CandidateProfileRow) -> dict[str, Any]:
        data = {field: getattr(row, field) for field in cls.PROFILE_FIELDS}
        data["preferences"] = dict(row.preferences or {})
        data["version"] = row.version
        return data

    def upsert_profile(
        self,
        fields: dict[str, Any],
        preferences: Optional[dict[str, Any]],
        actor: str,
        source_hash: Optional[str] = None,
    ) -> tuple[CandidateProfileRow, str]:
        """Create or update the tenant's profile. Returns ``(row, action)``.

        ``action`` is ``"created"``, ``"updated"`` or ``"skipped"`` (nothing
        differed). Unknown keys in ``fields`` are ignored rather than raised,
        so a Profile model dump can be passed straight through.
        """
        row = self.get_profile_row()
        clean = {k: v for k, v in fields.items() if k in self.PROFILE_FIELDS}
        self._check_profile_values(clean)
        if row is None:
            if not clean.get("name"):
                raise ValidationFailed("A profile needs at least a name.")
            self.ensure_tenant(clean.get("name", ""))
            row = CandidateProfileRow(
                tenant_id=self.tenant_id,
                preferences=dict(preferences or {}),
                source_hash=source_hash,
                **clean,
            )
            self.db.add(row)
            self.db.flush()
            self.record(
                "candidate_profile", row.id, "created", actor, None, self.profile_snapshot(row)
            )
            return row, "created"

        before = self.profile_snapshot(row)
        changed = False
        for key, value in clean.items():
            if getattr(row, key) != value:
                setattr(row, key, value)
                changed = True
        if preferences is not None and dict(row.preferences or {}) != dict(preferences):
            row.preferences = dict(preferences)
            changed = True
        if not changed:
            if source_hash and row.source_hash != source_hash:
                row.source_hash = source_hash
                self.db.flush()
            return row, "skipped"
        row.version += 1
        row.updated_at = db_now()
        if source_hash:
            row.source_hash = source_hash
        self.db.flush()
        self.record("candidate_profile", row.id, "updated", actor, before, self.profile_snapshot(row))
        return row, "updated"

    # ------------------------------------------------------------------ #
    # evidence nodes
    # ------------------------------------------------------------------ #

    @staticmethod
    def node_snapshot(row: EvidenceNodeRow) -> dict[str, Any]:
        return {
            "key": row.key,
            "kind": row.kind,
            "label": row.label,
            "claim": row.claim,
            "attributes": dict(row.attributes or {}),
            "verification_status": row.verification_status,
            "confidence": row.confidence,
            "allowed_for_resume": row.allowed_for_resume,
            "allowed_for_application": row.allowed_for_application,
            "source_type": row.source_type,
            "source_ref": row.source_ref,
            "artifact_ref": row.artifact_ref,
            "verified_by": row.verified_by,
            "verified_at": _iso(row.verified_at),
            "status": row.status,
            "sort_order": row.sort_order,
            "version": row.version,
        }

    def _node_query(self, include_removed: bool = False):
        query = self.db.query(EvidenceNodeRow).filter(EvidenceNodeRow.tenant_id == self.tenant_id)
        if not include_removed:
            query = query.filter(EvidenceNodeRow.status == EvidenceStatus.ACTIVE.value)
        return query

    def list_nodes(
        self, kind: Optional[EvidenceKind] = None, include_removed: bool = False
    ) -> list[EvidenceNodeRow]:
        query = self._node_query(include_removed)
        if kind is not None:
            query = query.filter(EvidenceNodeRow.kind == kind.value)
        return query.order_by(EvidenceNodeRow.sort_order, EvidenceNodeRow.created_at).all()

    def get_node(self, key: str, include_removed: bool = True) -> Optional[EvidenceNodeRow]:
        return self._node_query(include_removed).filter(EvidenceNodeRow.key == key).first()

    def get_nodes_by_keys(self, keys: list[str], include_removed: bool = False) -> dict[str, EvidenceNodeRow]:
        if not keys:
            return {}
        rows = self._node_query(include_removed).filter(EvidenceNodeRow.key.in_(list(keys))).all()
        return {row.key: row for row in rows}

    def require_node(self, key: str, include_removed: bool = True) -> EvidenceNodeRow:
        row = self.get_node(key, include_removed=include_removed)
        if row is None:
            raise NotFoundError(f"Evidence node not found: {key}", details={"key": key})
        return row

    def create_node(
        self,
        data: EvidenceNodeCreate,
        actor: str,
        source_hash: Optional[str] = None,
    ) -> EvidenceNodeRow:
        key = data.key or f"{data.kind.value.lower()}:{slugify(data.label)}"
        if self.get_node(key, include_removed=True) is not None:
            raise ConflictError(
                f"Evidence node already exists: {key}", details={"key": key}
            )
        if data.source_type in UNTRUSTED_SOURCE_TYPES and (
            data.verification_status is VerificationStatus.VERIFIED
        ):
            raise ValidationFailed(
                "Extraction is not verification: evidence from "
                f"{data.source_type.value} cannot be created as VERIFIED. "
                "Create it as NEEDS_REVIEW and confirm it explicitly.",
                details={"key": key, "source_type": data.source_type.value},
            )
        verified = data.verification_status is VerificationStatus.VERIFIED
        allowed_resume = data.allowed_for_resume if data.allowed_for_resume is not None else verified
        allowed_app = (
            data.allowed_for_application if data.allowed_for_application is not None else verified
        )
        check_truth_rules(data.verification_status, allowed_resume, allowed_app)

        self.ensure_tenant()
        now = db_now()
        row = EvidenceNodeRow(
            tenant_id=self.tenant_id,
            key=key,
            kind=data.kind.value,
            label=data.label[:256],
            claim=data.claim,
            attributes=dict(data.attributes),
            verification_status=data.verification_status.value,
            confidence=data.confidence,
            allowed_for_resume=allowed_resume,
            allowed_for_application=allowed_app,
            source_type=data.source_type.value,
            source_ref=data.source_ref,
            source_hash=source_hash,
            artifact_ref=data.artifact_ref,
            verified_by=actor if verified else None,
            verified_at=now if verified else None,
            status=EvidenceStatus.ACTIVE.value,
            sort_order=data.sort_order,
            version=1,
        )
        self.db.add(row)
        self.db.flush()
        self.record("evidence_node", key, "created", actor, None, self.node_snapshot(row))
        return row

    def update_node(
        self,
        key: str,
        data: EvidenceNodeUpdate,
        actor: str,
        source_hash: Optional[str] = None,
    ) -> EvidenceNodeRow:
        row = self.require_node(key)
        before = self.node_snapshot(row)
        changes = data.model_dump(exclude_unset=True)

        new_status = (
            VerificationStatus(changes["verification_status"])
            if changes.get("verification_status") is not None
            else VerificationStatus(row.verification_status)
        )
        status_changed = new_status.value != row.verification_status
        verified_now = new_status is VerificationStatus.VERIFIED

        if "allowed_for_resume" in changes and changes["allowed_for_resume"] is not None:
            allowed_resume = changes["allowed_for_resume"]
        elif status_changed:
            allowed_resume = verified_now
        else:
            allowed_resume = row.allowed_for_resume
        if "allowed_for_application" in changes and changes["allowed_for_application"] is not None:
            allowed_app = changes["allowed_for_application"]
        elif status_changed:
            allowed_app = verified_now
        else:
            allowed_app = row.allowed_for_application
        check_truth_rules(new_status, allowed_resume, allowed_app)

        for field in ("label", "claim", "attributes", "confidence", "source_ref", "artifact_ref", "sort_order"):
            if field in changes and changes[field] is not None:
                value = changes[field]
                if field == "label":
                    value = value[:256]
                if field == "attributes":
                    value = dict(value)
                setattr(row, field, value)
        if changes.get("source_type") is not None:
            row.source_type = EvidenceSourceType(changes["source_type"]).value

        row.verification_status = new_status.value
        row.allowed_for_resume = allowed_resume
        row.allowed_for_application = allowed_app
        if status_changed:
            if verified_now:
                # The human (or trusted importer) who confirmed it is recorded.
                row.verified_by = actor
                row.verified_at = db_now()
            else:
                row.verified_by = None
                row.verified_at = None
        if source_hash is not None:
            row.source_hash = source_hash

        after = self.node_snapshot(row)
        after.pop("version")
        before_cmp = dict(before)
        before_cmp.pop("version")
        if after == before_cmp:
            return row  # nothing changed: no version bump, no audit noise
        row.version += 1
        row.updated_at = db_now()
        self.db.flush()
        self.record("evidence_node", key, "updated", actor, before, self.node_snapshot(row))
        return row

    def remove_node(self, key: str, actor: str, reason: Optional[str] = None) -> EvidenceNodeRow:
        row = self.require_node(key)
        if row.status == EvidenceStatus.REMOVED.value:
            return row
        before = self.node_snapshot(row)
        row.status = EvidenceStatus.REMOVED.value
        row.removed_at = db_now()
        row.version += 1
        row.updated_at = db_now()
        self.db.flush()
        self.record("evidence_node", key, "removed", actor, before, self.node_snapshot(row), reason)
        return row

    def restore_node(self, key: str, actor: str) -> EvidenceNodeRow:
        row = self.require_node(key)
        if row.status == EvidenceStatus.ACTIVE.value:
            return row
        before = self.node_snapshot(row)
        row.status = EvidenceStatus.ACTIVE.value
        row.removed_at = None
        row.version += 1
        row.updated_at = db_now()
        self.db.flush()
        self.record("evidence_node", key, "restored", actor, before, self.node_snapshot(row))
        return row

    # ------------------------------------------------------------------ #
    # relationships
    # ------------------------------------------------------------------ #

    def add_relationship(
        self,
        from_key: str,
        to_key: str,
        relation: RelationType,
        actor: str,
        position: int = 0,
    ) -> tuple[EvidenceRelationshipRow, bool]:
        """Link two nodes of this tenant. Returns ``(row, created)``; idempotent."""
        from_row = self.require_node(from_key)
        to_row = self.require_node(to_key)
        existing = (
            self.db.query(EvidenceRelationshipRow)
            .filter(
                EvidenceRelationshipRow.from_node_id == from_row.id,
                EvidenceRelationshipRow.to_node_id == to_row.id,
                EvidenceRelationshipRow.relation == relation.value,
            )
            .first()
        )
        if existing is not None:
            if existing.position != position:
                existing.position = position
                self.db.flush()
            return existing, False
        row = EvidenceRelationshipRow(
            tenant_id=self.tenant_id,
            from_node_id=from_row.id,
            to_node_id=to_row.id,
            relation=relation.value,
            position=position,
        )
        self.db.add(row)
        self.db.flush()
        self.record(
            "evidence_relationship",
            f"{from_key}->{to_key}",
            "created",
            actor,
            None,
            {"from": from_key, "to": to_key, "relation": relation.value, "position": position},
        )
        return row, True

    def remove_relationship(
        self, from_key: str, to_key: str, relation: RelationType, actor: str
    ) -> bool:
        from_row = self.require_node(from_key)
        to_row = self.require_node(to_key)
        row = (
            self.db.query(EvidenceRelationshipRow)
            .filter(
                EvidenceRelationshipRow.from_node_id == from_row.id,
                EvidenceRelationshipRow.to_node_id == to_row.id,
                EvidenceRelationshipRow.relation == relation.value,
            )
            .first()
        )
        if row is None:
            return False
        self.db.delete(row)
        self.db.flush()
        self.record(
            "evidence_relationship",
            f"{from_key}->{to_key}",
            "deleted",
            actor,
            {"from": from_key, "to": to_key, "relation": relation.value},
            None,
        )
        return True

    def list_relationships(self, key: Optional[str] = None) -> list[EvidenceRelationshipRow]:
        query = self.db.query(EvidenceRelationshipRow).filter(
            EvidenceRelationshipRow.tenant_id == self.tenant_id
        )
        if key is not None:
            node = self.require_node(key)
            query = query.filter(
                or_(
                    EvidenceRelationshipRow.from_node_id == node.id,
                    EvidenceRelationshipRow.to_node_id == node.id,
                )
            )
        return query.order_by(EvidenceRelationshipRow.position, EvidenceRelationshipRow.created_at).all()

    # ------------------------------------------------------------------ #
    # positioning variants
    # ------------------------------------------------------------------ #

    @staticmethod
    def variant_snapshot(row: PositioningVariantRow) -> dict[str, Any]:
        return {
            "role_family": row.role_family,
            "name": row.name,
            "headline": row.headline,
            "summary": row.summary,
            "is_active": row.is_active,
            "version": row.version,
            "evidence": [
                {"key": e.node.key, "position": e.position, "section": e.section}
                for e in sorted(row.evidence, key=lambda e: e.position)
            ],
        }

    def _resolve_evidence_refs(self, refs: list[PositioningVariantEvidence]) -> list[tuple[EvidenceNodeRow, PositioningVariantEvidence]]:
        """Every reference must be an active node of this tenant."""
        keys = [ref.key for ref in refs]
        if len(set(keys)) != len(keys):
            raise ValidationFailed("Duplicate evidence keys in variant", details={"keys": keys})
        rows = self.get_nodes_by_keys(keys, include_removed=False)
        missing = [k for k in keys if k not in rows]
        if missing:
            raise ValidationFailed(
                "Positioning variants may only reference existing, active evidence; "
                f"unknown or removed: {', '.join(missing)}",
                details={"missing": missing},
            )
        return [(rows[ref.key], ref) for ref in refs]

    def list_variants(
        self, role_family: Optional[str] = None, active_only: bool = False
    ) -> list[PositioningVariantRow]:
        query = self.db.query(PositioningVariantRow).filter(
            PositioningVariantRow.tenant_id == self.tenant_id
        )
        if role_family:
            query = query.filter(PositioningVariantRow.role_family == role_family)
        if active_only:
            query = query.filter(PositioningVariantRow.is_active.is_(True))
        return query.order_by(PositioningVariantRow.role_family, PositioningVariantRow.created_at).all()

    def get_variant(self, variant_id: str) -> Optional[PositioningVariantRow]:
        return (
            self.db.query(PositioningVariantRow)
            .filter(
                PositioningVariantRow.tenant_id == self.tenant_id,
                PositioningVariantRow.id == variant_id,
            )
            .first()
        )

    def require_variant(self, variant_id: str) -> PositioningVariantRow:
        row = self.get_variant(variant_id)
        if row is None:
            raise NotFoundError(f"Positioning variant not found: {variant_id}")
        return row

    def create_variant(self, data: PositioningVariantCreate, actor: str) -> PositioningVariantRow:
        resolved = self._resolve_evidence_refs(data.evidence)
        name = data.name or data.role_family
        duplicate = (
            self.db.query(PositioningVariantRow)
            .filter(
                PositioningVariantRow.tenant_id == self.tenant_id,
                PositioningVariantRow.role_family == data.role_family,
                PositioningVariantRow.name == name,
            )
            .first()
        )
        if duplicate is not None:
            raise ConflictError(
                f"A variant named '{name}' already exists for {data.role_family}"
            )
        self.ensure_tenant()
        row = PositioningVariantRow(
            tenant_id=self.tenant_id,
            role_family=data.role_family,
            name=name,
            headline=data.headline,
            summary=data.summary,
            is_active=data.is_active,
        )
        for node, ref in resolved:
            row.evidence.append(
                PositioningVariantEvidenceRow(node=node, position=ref.position, section=ref.section)
            )
        self.db.add(row)
        self.db.flush()
        self.record("positioning_variant", row.id, "created", actor, None, self.variant_snapshot(row))
        return row

    def update_variant(
        self, variant_id: str, data: PositioningVariantUpdate, actor: str
    ) -> PositioningVariantRow:
        row = self.require_variant(variant_id)
        before = self.variant_snapshot(row)
        changes = data.model_dump(exclude_unset=True)
        for field in ("role_family", "name", "headline", "summary", "is_active"):
            if field in changes and changes[field] is not None:
                setattr(row, field, changes[field])
        if data.evidence is not None:
            resolved = self._resolve_evidence_refs(data.evidence)
            row.evidence.clear()
            self.db.flush()
            for node, ref in resolved:
                row.evidence.append(
                    PositioningVariantEvidenceRow(
                        node=node, position=ref.position, section=ref.section
                    )
                )
        self.db.flush()
        after = self.variant_snapshot(row)
        if {k: v for k, v in after.items() if k != "version"} == {
            k: v for k, v in before.items() if k != "version"
        }:
            return row
        row.version += 1
        row.updated_at = db_now()
        self.db.flush()
        self.record("positioning_variant", row.id, "updated", actor, before, self.variant_snapshot(row))
        return row

    def delete_variant(self, variant_id: str, actor: str) -> None:
        row = self.require_variant(variant_id)
        before = self.variant_snapshot(row)
        self.db.delete(row)
        self.db.flush()
        self.record("positioning_variant", variant_id, "deleted", actor, before, None)

    # ------------------------------------------------------------------ #
    # answer bank
    # ------------------------------------------------------------------ #

    @staticmethod
    def answer_snapshot(row: AnswerBankEntryRow) -> dict[str, Any]:
        return {
            "category": row.category,
            "question": row.question,
            "answer": row.answer,
            "evidence_keys": list(row.evidence_keys or []),
            "status": row.status,
            "version": row.version,
            "approved_at": _iso(row.approved_at),
        }

    def _check_answer_evidence(self, keys: list[str]) -> None:
        rows = self.get_nodes_by_keys(keys, include_removed=False)
        missing = [k for k in keys if k not in rows]
        if missing:
            raise ValidationFailed(
                f"Answer references unknown or removed evidence: {', '.join(missing)}",
                details={"missing": missing},
            )

    def list_answers(
        self, category: Optional[str] = None, status: Optional[AnswerStatus] = None
    ) -> list[AnswerBankEntryRow]:
        query = self.db.query(AnswerBankEntryRow).filter(
            AnswerBankEntryRow.tenant_id == self.tenant_id
        )
        if category:
            query = query.filter(AnswerBankEntryRow.category == category)
        if status is not None:
            query = query.filter(AnswerBankEntryRow.status == status.value)
        return query.order_by(AnswerBankEntryRow.category, AnswerBankEntryRow.created_at).all()

    def get_answer(self, entry_id: str) -> Optional[AnswerBankEntryRow]:
        return (
            self.db.query(AnswerBankEntryRow)
            .filter(
                AnswerBankEntryRow.tenant_id == self.tenant_id,
                AnswerBankEntryRow.id == entry_id,
            )
            .first()
        )

    def require_answer(self, entry_id: str) -> AnswerBankEntryRow:
        row = self.get_answer(entry_id)
        if row is None:
            raise NotFoundError(f"Answer bank entry not found: {entry_id}")
        return row

    def find_answer(self, question: str) -> Optional[AnswerBankEntryRow]:
        """Approved answer for an exact (normalised) question, or ``None``.

        Deterministic on purpose: no fuzzy matching, no model call. Novel
        questions are the caller's problem (review queue or AI, later phases).
        """
        return (
            self.db.query(AnswerBankEntryRow)
            .filter(
                AnswerBankEntryRow.tenant_id == self.tenant_id,
                AnswerBankEntryRow.question_key == normalize_question(question),
                AnswerBankEntryRow.status == AnswerStatus.APPROVED.value,
            )
            .first()
        )

    def create_answer(self, data: AnswerBankEntryCreate, actor: str) -> AnswerBankEntryRow:
        question_key = normalize_question(data.question)
        if not question_key:
            raise ValidationFailed("Question cannot be empty")
        existing = (
            self.db.query(AnswerBankEntryRow)
            .filter(
                AnswerBankEntryRow.tenant_id == self.tenant_id,
                AnswerBankEntryRow.question_key == question_key,
            )
            .first()
        )
        if existing is not None:
            raise ConflictError(
                "An answer for this question already exists", details={"id": existing.id}
            )
        self._check_answer_evidence(data.evidence_keys)
        self.ensure_tenant()
        row = AnswerBankEntryRow(
            tenant_id=self.tenant_id,
            category=data.category,
            question=data.question,
            question_key=question_key,
            answer=data.answer,
            evidence_keys=list(data.evidence_keys),
            status=data.status.value,
            approved_at=db_now() if data.status is AnswerStatus.APPROVED else None,
        )
        self.db.add(row)
        self.db.flush()
        self.record("answer_bank_entry", row.id, "created", actor, None, self.answer_snapshot(row))
        return row

    def update_answer(
        self, entry_id: str, data: AnswerBankEntryUpdate, actor: str
    ) -> AnswerBankEntryRow:
        row = self.require_answer(entry_id)
        before = self.answer_snapshot(row)
        changes = data.model_dump(exclude_unset=True)
        if changes.get("question") is not None:
            new_key = normalize_question(changes["question"])
            clash = (
                self.db.query(AnswerBankEntryRow)
                .filter(
                    AnswerBankEntryRow.tenant_id == self.tenant_id,
                    AnswerBankEntryRow.question_key == new_key,
                    AnswerBankEntryRow.id != row.id,
                )
                .first()
            )
            if clash is not None:
                raise ConflictError("Another entry already answers this question")
            row.question = changes["question"]
            row.question_key = new_key
        if changes.get("category") is not None:
            row.category = changes["category"]
        if changes.get("evidence_keys") is not None:
            self._check_answer_evidence(changes["evidence_keys"])
            row.evidence_keys = list(changes["evidence_keys"])
        content_changed = False
        if changes.get("answer") is not None and changes["answer"] != row.answer:
            row.answer = changes["answer"]
            content_changed = True
        if changes.get("status") is not None:
            new_status = AnswerStatus(changes["status"])
            if new_status is AnswerStatus.APPROVED and row.status != AnswerStatus.APPROVED.value:
                row.approved_at = db_now()
            row.status = new_status.value
        elif content_changed and row.status == AnswerStatus.APPROVED.value:
            # An edited answer is no longer the approved text.
            row.status = AnswerStatus.DRAFT.value
            row.approved_at = None
        self.db.flush()
        after = self.answer_snapshot(row)
        if {k: v for k, v in after.items() if k != "version"} == {
            k: v for k, v in before.items() if k != "version"
        }:
            return row
        row.version += 1
        row.updated_at = db_now()
        self.db.flush()
        self.record("answer_bank_entry", row.id, "updated", actor, before, self.answer_snapshot(row))
        return row

    def approve_answer(self, entry_id: str, actor: str) -> AnswerBankEntryRow:
        return self.update_answer(
            entry_id, AnswerBankEntryUpdate(status=AnswerStatus.APPROVED), actor
        )

    def delete_answer(self, entry_id: str, actor: str) -> None:
        row = self.require_answer(entry_id)
        before = self.answer_snapshot(row)
        self.db.delete(row)
        self.db.flush()
        self.record("answer_bank_entry", entry_id, "deleted", actor, before, None)
