"""DocumentService: render, reuse, verify, invalidate document artifacts.

Identity of an artifact = SHA-256 over the preparation's blocks, the contact
header, the job facts on the model, the layout options, the renderer name
and version. Same identity → the existing ACTIVE file is reused (and
re-verified); anything else → a new immutable version. Invalidation marks
the row; the file stays for audit and for attempts that referenced it.
"""

import hashlib
import json
import logging
from typing import Any, Optional

from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.core.errors import CareerOSError, ConflictError, NotFoundError, ValidationFailed
from app.core.timeutils import db_now
from app.documents.content import contact_from_profile, cover_letter_model, resume_model
from app.documents.database.models import DocumentArtifactRow
from app.documents.models import (
    DocumentArtifact,
    DocumentFormat,
    DocumentModel,
    DocumentStatus,
    DocumentValidation,
    RenderInput,
    RenderReport,
)
from app.documents.renderers.base import Renderer, RenderError
from app.documents.renderers.docx import DocxRenderer
from app.documents.renderers.pdf import PdfRenderer
from app.documents.storage import ArtifactStore, StorageError, sha256_bytes
from app.documents.validate import validate_document
from app.execution.package import load_profile_facts
from app.pipeline.database.models import OpportunityRow
from app.pipeline.repository import OpportunityRepository
from app.preparation.database.models import ApplicationPreparationRow
from app.preparation.models import ArtifactKind, CoverLetterMode, PreparationStatus

logger = logging.getLogger(__name__)

#: How many orphaned write-once files (left by rolled-back renders) a render may step past.
ORPHAN_VERSION_SKIP_LIMIT = 50


class DocumentError(CareerOSError):
    code = "document_error"
    http_status = 422


def default_renderers() -> dict[DocumentFormat, Renderer]:
    return {DocumentFormat.PDF: PdfRenderer(), DocumentFormat.DOCX: DocxRenderer()}


class DocumentService:
    def __init__(self, db: Session, tenant_id: str, actor: str = "documents", store: Optional[ArtifactStore] = None, renderers: Optional[dict[DocumentFormat, Renderer]] = None):
        if not tenant_id:
            raise ValidationFailed("tenant_id is required")
        self.db = db
        self.tenant_id = tenant_id
        self.actor = actor
        self.store = store or ArtifactStore()
        self.renderers = renderers or default_renderers()
        self.repo = OpportunityRepository(db, tenant_id)
        self._profile: Optional[dict[str, Any]] = None
        self.stats = {"rendered": 0, "reused": 0, "invalidated": 0}

    # ---------------------------------------------------------------- reads

    def _query(self):
        return self.db.query(DocumentArtifactRow).filter(DocumentArtifactRow.tenant_id == self.tenant_id)

    def get(self, artifact_id: str) -> Optional[DocumentArtifactRow]:
        return self._query().filter(DocumentArtifactRow.id == artifact_id).first()

    def require(self, artifact_id: str) -> DocumentArtifactRow:
        row = self.get(artifact_id)
        if row is None:
            raise NotFoundError(f"Document artifact not found: {artifact_id}")
        return row

    def list_for(self, preparation_id: str) -> list[DocumentArtifactRow]:
        return self._query().filter(DocumentArtifactRow.preparation_id == preparation_id).order_by(DocumentArtifactRow.artifact_type, DocumentArtifactRow.format, DocumentArtifactRow.version.desc()).all()

    def active(self, preparation_id: str, artifact_type: str, fmt: DocumentFormat) -> Optional[DocumentArtifactRow]:
        return (
            self._query()
            .filter(DocumentArtifactRow.preparation_id == preparation_id, DocumentArtifactRow.artifact_type == artifact_type, DocumentArtifactRow.format == fmt.value, DocumentArtifactRow.status == DocumentStatus.ACTIVE.value)
            .order_by(DocumentArtifactRow.version.desc())
            .first()
        )

    def _preparation(self, preparation_id: str) -> ApplicationPreparationRow:
        row = (
            self.db.query(ApplicationPreparationRow)
            .options(joinedload(ApplicationPreparationRow.artifacts))
            .filter(ApplicationPreparationRow.tenant_id == self.tenant_id, ApplicationPreparationRow.id == preparation_id)
            .first()
        )
        if row is None:
            raise NotFoundError(f"Preparation not found: {preparation_id}")
        return row

    # ---------------------------------------------------------------- model

    def model_for(self, preparation: ApplicationPreparationRow, artifact_type: str) -> Optional[DocumentModel]:
        """The DocumentModel for a preparation artifact, or None when absent."""
        source = next((a for a in preparation.artifacts if a.artifact_type == artifact_type), None)
        if source is None:
            return None
        if self._profile is None:
            self._profile = load_profile_facts(self.db, self.tenant_id)
        contact = contact_from_profile(self._profile)
        opportunity = self.db.get(OpportunityRow, preparation.opportunity_id)
        job_title = opportunity.title if opportunity else None
        company = opportunity.company if opportunity else None
        blocks = list(source.blocks or [])
        if artifact_type == ArtifactKind.RESUME.value:
            return resume_model(blocks, contact, job_title)
        if preparation.cover_letter_mode == CoverLetterMode.DISABLED.value:
            return None
        return cover_letter_model(blocks, contact, company, job_title, settings.documents_letter_date)

    def fingerprint(self, preparation: ApplicationPreparationRow, model: DocumentModel, renderer: Renderer, options: Optional[dict[str, Any]] = None) -> str:
        payload = {
            "preparation": preparation.id,
            "preparation_version": preparation.version,
            "preparation_fingerprint": preparation.input_fingerprint,
            "model": model.model_dump(mode="json"),
            "options": options or {},
            "renderer": renderer.name,
            "renderer_version": renderer.version,
            # The font actually used is part of the identity (core font vs embedded TrueType).
            "font": str(renderer.font_for(model) or "core") if hasattr(renderer, "font_for") else "",
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    # --------------------------------------------------------------- render

    def get_or_render(self, preparation_id: str, artifact_type: str, fmt: DocumentFormat = DocumentFormat.PDF, force: bool = False, options: Optional[dict[str, Any]] = None) -> tuple[DocumentArtifactRow, bool]:
        """Return the current artifact, rendering a new version only when needed.

        Reuse requires: an ACTIVE row with the same input fingerprint whose
        file still verifies (hash, size, magic). A verified-corrupt file is
        invalidated and a fresh version rendered.
        """
        preparation = self._preparation(preparation_id)
        if preparation.status not in (PreparationStatus.READY.value,):
            raise DocumentError(f"preparation is {preparation.status}; only READY preparations are rendered")
        renderer = self.renderers.get(fmt)
        if renderer is None:
            raise DocumentError(f"no renderer for {fmt.value}")
        model = self.model_for(preparation, artifact_type)
        if model is None:
            raise DocumentError(f"preparation has no {artifact_type} content to render")
        fingerprint = self.fingerprint(preparation, model, renderer, options)
        current = self.active(preparation_id, artifact_type, fmt)
        if current is not None and not force:
            if current.input_fingerprint == fingerprint:
                problems = self.store.verify(current.relative_path, current.content_hash, current.byte_size, fmt)
                if not problems:
                    self.stats["reused"] += 1
                    return current, False
                self.invalidate(current.id, "stored file failed verification: " + "; ".join(problems), actor=self.actor)
            else:
                self.invalidate(current.id, "preparation, contact or renderer changed", actor=self.actor)
        elif current is not None and force:
            self.invalidate(current.id, "regenerated on request", actor=self.actor)
        render_input = RenderInput(
            tenant_id=self.tenant_id,
            preparation_id=preparation.id,
            preparation_version=preparation.version,
            preparation_fingerprint=preparation.input_fingerprint,
            evidence_fingerprint=(preparation.inputs or {}).get("evidence_fingerprint"),
            artifact_type=artifact_type,
            format=fmt,
            model=model,
            options=dict(options or {}),
        )
        if not renderer.can_render(render_input):
            raise DocumentError(f"{renderer.name} cannot render this {artifact_type} as {fmt.value}")
        try:
            result = renderer.render(render_input)
        except RenderError as exc:
            raise DocumentError(f"render failed: {exc}") from exc
        outcome = validate_document(result.data, fmt, model)
        if outcome.status is DocumentValidation.FAILED:
            raise DocumentError("rendered document failed validation: " + "; ".join(outcome.issues[:3]))
        latest = (
            self._query()
            .filter(DocumentArtifactRow.preparation_id == preparation_id, DocumentArtifactRow.artifact_type == artifact_type, DocumentArtifactRow.format == fmt.value)
            .order_by(DocumentArtifactRow.version.desc())
            .first()
        )
        version = (latest.version + 1) if latest else 1
        relative = self.store.relative_path(self.tenant_id, preparation.candidate_opportunity_id, preparation.id, artifact_type, fmt, version)
        # Audit fix (2026-09-14): a render whose transaction was rolled back (a later
        # failure in the same request or execution start) leaves its write-once file
        # with no row. The next render computed the same version, hit "artifact already
        # exists" and every later render of that preparation failed the same way. An
        # orphaned file is kept untouched (never overwritten); the version moves past it.
        skipped = 0
        while self.store.absolute(relative).exists() and skipped < ORPHAN_VERSION_SKIP_LIMIT:
            skipped += 1
            version += 1
            relative = self.store.relative_path(self.tenant_id, preparation.candidate_opportunity_id, preparation.id, artifact_type, fmt, version)
        if skipped:
            logger.warning("Skipped %d orphaned %s %s file(s) for preparation %s", skipped, artifact_type, fmt.value, preparation.id)
        try:
            self.store.write_immutable(relative, result.data)
        except StorageError as exc:
            raise DocumentError(exc.message) from exc
        row = DocumentArtifactRow(
            tenant_id=self.tenant_id,
            candidate_opportunity_id=preparation.candidate_opportunity_id,
            preparation_id=preparation.id,
            preparation_version=preparation.version,
            artifact_type=artifact_type,
            format=fmt.value,
            version=version,
            status=DocumentStatus.ACTIVE.value,
            validation_status=outcome.status.value,
            validation_report=outcome.model_dump(mode="json"),
            content_hash=sha256_bytes(result.data),
            input_fingerprint=fingerprint,
            evidence_fingerprint=render_input.evidence_fingerprint,
            renderer=renderer.name,
            renderer_version=renderer.version,
            byte_size=len(result.data),
            page_count=outcome.page_count,
            relative_path=relative,
        )
        self.db.add(row)
        self.db.flush()
        self.stats["rendered"] += 1
        # Audit facts only: never the document text.
        self.repo.record("document_artifact", row.id, "rendered", self.actor, None, {"preparation_id": preparation.id, "type": artifact_type, "format": fmt.value, "version": version, "sha256": row.content_hash, "bytes": row.byte_size, "pages": row.page_count, "validation": row.validation_status})
        return row, True

    def ensure_for_execution(self, preparation: ApplicationPreparationRow, fmt: DocumentFormat = DocumentFormat.PDF) -> RenderReport:
        """The documents an execution needs: resume always, cover letter when enabled."""
        report = RenderReport(preparation_id=preparation.id)
        wanted = [ArtifactKind.RESUME.value]
        if preparation.cover_letter_mode != CoverLetterMode.DISABLED.value and any(a.artifact_type == ArtifactKind.COVER_LETTER.value for a in preparation.artifacts):
            wanted.append(ArtifactKind.COVER_LETTER.value)
        for artifact_type in wanted:
            try:
                row, created = self.get_or_render(preparation.id, artifact_type, fmt)
            except CareerOSError as exc:
                report.problems.append(f"{artifact_type}: {exc.message}")
                continue
            if row.validation_status != DocumentValidation.PASSED.value:
                report.problems.append(f"{artifact_type}: rendered document is {row.validation_status}: " + "; ".join((row.validation_report or {}).get("issues", [])[:2]))
                continue
            report.artifacts.append(DocumentArtifact.model_validate(row))
            report.created += int(created)
            report.reused += int(not created)
        return report

    # ------------------------------------------------------------ materialize

    def materialize(self, artifact_id: str, preparation_id: Optional[str] = None, for_upload: bool = True) -> tuple[DocumentArtifactRow, str]:
        """The verified local path for an ACTIVE artifact (raises otherwise).

        ``for_upload`` (default) also refuses NEEDS_REVIEW artifacts: a
        document that tripped a layout check is looked at by a person, never
        uploaded silently.
        """
        row = self.require(artifact_id)
        if row.status != DocumentStatus.ACTIVE.value:
            raise DocumentError(f"artifact is {row.status}")
        if preparation_id is not None and row.preparation_id != preparation_id:
            raise DocumentError("artifact belongs to another preparation")
        if row.validation_status == DocumentValidation.FAILED.value:
            raise DocumentError("artifact failed validation")
        if for_upload and row.validation_status == DocumentValidation.NEEDS_REVIEW.value:
            raise DocumentError("artifact needs review before it can be uploaded: " + "; ".join((row.validation_report or {}).get("issues", [])[:2]))
        problems = self.store.verify(row.relative_path, row.content_hash, row.byte_size, DocumentFormat(row.format))
        if problems:
            self.invalidate(row.id, "; ".join(problems), actor=self.actor)
            raise DocumentError("artifact file is not intact: " + "; ".join(problems))
        return row, str(self.store.absolute(row.relative_path))

    def read_bytes(self, artifact_id: str) -> tuple[DocumentArtifactRow, bytes]:
        row, path = self.materialize(artifact_id)
        with open(path, "rb") as handle:
            return row, handle.read()

    def invalidate(self, artifact_id: str, reason: str, actor: Optional[str] = None) -> DocumentArtifactRow:
        row = self.require(artifact_id)
        if row.status == DocumentStatus.INVALIDATED.value:
            return row
        row.status = DocumentStatus.INVALIDATED.value
        row.invalidated_at = db_now()
        row.invalidated_reason = (reason or "")[:256] or None
        self.db.flush()
        self.stats["invalidated"] += 1
        self.repo.record("document_artifact", row.id, "invalidated", actor or self.actor, {"status": "ACTIVE"}, {"status": row.status}, reason)
        return row

    def regenerate(self, preparation_id: str, artifact_type: str, fmt: DocumentFormat = DocumentFormat.PDF) -> DocumentArtifactRow:
        row, _ = self.get_or_render(preparation_id, artifact_type, fmt, force=True)
        return row

    def invalidate_for_preparation(self, preparation_id: str, reason: str) -> int:
        count = 0
        for row in self.list_for(preparation_id):
            if row.status == DocumentStatus.ACTIVE.value:
                self.invalidate(row.id, reason)
                count += 1
        return count


def artifact_conflict(row: DocumentArtifactRow, tenant_id: str, preparation_id: str) -> Optional[str]:
    if row.tenant_id != tenant_id:
        return "artifact belongs to another tenant"
    if row.preparation_id != preparation_id:
        return "artifact belongs to another preparation"
    if row.status != DocumentStatus.ACTIVE.value:
        return f"artifact is {row.status}"
    return None


__all__ = ["DocumentService", "DocumentError", "artifact_conflict", "ConflictError"]
