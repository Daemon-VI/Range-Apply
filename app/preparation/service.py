"""PreparationService: build, version, validate and audit application packages.

Scope: one tenant. Inputs are batch-loaded (evidence snapshot once, then
candidate opportunities, jobs, matches and assessments in IN queries), so
``prepare_many`` over a thousand opportunities costs a fixed handful of
SELECTs plus the inserts for the packages themselves.

Idempotency: a package is keyed by an ``input_fingerprint`` over evidence
version, job content hash, match, level/lane/cover mode, variant version,
policy version, template version, question set and AI configuration. The
same inputs return the existing package; changed inputs create a new
version and mark the previous one SUPERSEDED.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.orm import Session, joinedload

from app.career.database.models import PositioningVariantRow
from app.career.models import AnswerBankEntryCreate, AnswerStatus
from app.career.repository import EvidenceRepository, normalize_question
from app.core.errors import ConflictError, NotFoundError, PolicyBlocked, ValidationFailed
from app.core.ids import new_id
from app.core.timeutils import db_now
from app.intelligence.database.models import JobMatchRow, RequirementAssessmentRow
from app.jobs.database.models import JobRow
from app.pipeline.database.models import CandidateOpportunityRow
from app.pipeline.models import ApplicationPolicy, Lane, OpportunityState, TailoringLevel
from app.pipeline.repository import OpportunityRepository, PolicyRepository
from app.preparation.ai import StubPolisher, TextPolisher, get_polisher
from app.preparation.composer import compose_cover_letter, compose_resume, render
from app.preparation.database.models import (
    ApplicationPreparationRow,
    PreparationAnswerRow,
    PreparationArtifactRow,
)
from app.preparation.evidence import EvidenceSelection, EvidenceSnapshot, select_evidence
from app.preparation.models import (
    TEMPLATE_VERSION,
    AnswerSource,
    ArtifactKind,
    Block,
    BlockKind,
    CoverLetterMode,
    PreparationStatus,
    PreparedAnswerStatus,
    PrepareManyReport,
    ValidationReport,
    ValidationStatus,
)
from app.preparation.positioning import PositioningChoice, select_variant
from app.preparation.questions import DEFAULT_QUESTIONS, AnswerResolver, QuestionCategory
from app.preparation.validator import PreparationValidator

logger = logging.getLogger(__name__)

DEFAULT_ACTOR = "preparation"
#: How many CLAIM blocks an L2 package offers to the polisher.
L2_POLISH_BLOCKS = 6
#: States a READY package may move a candidate opportunity from.
_PREPARED_FROM = frozenset(
    {
        OpportunityState.ELIGIBLE,
        OpportunityState.UNCERTAIN,
        OpportunityState.SHORTLISTED,
        OpportunityState.QUEUED,
        OpportunityState.IN_REVIEW,
    }
)
_REVIEW_FROM = frozenset(
    {
        OpportunityState.ELIGIBLE,
        OpportunityState.UNCERTAIN,
        OpportunityState.SHORTLISTED,
        OpportunityState.QUEUED,
        OpportunityState.PREPARED,
    }
)


@dataclass
class PreparationContext:
    snapshot: EvidenceSnapshot
    policy: ApplicationPolicy
    validator: PreparationValidator
    resolver: AnswerResolver
    polisher: TextPolisher


@dataclass
class Target:
    co: CandidateOpportunityRow
    job: Optional[JobRow]
    match: Optional[JobMatchRow]
    assessments: list[RequirementAssessmentRow] = field(default_factory=list)


@dataclass
class Build:
    level: TailoringLevel
    lane: Lane
    cover_mode: CoverLetterMode
    choice: PositioningChoice
    selection: EvidenceSelection
    resume_blocks: list[Block]
    cover_blocks: Optional[list[Block]]
    answers: list[dict[str, Any]]
    report: ValidationReport
    validation_status: ValidationStatus
    status: PreparationStatus
    ai_calls: int = 0
    ai_used: bool = False
    #: Phase 8b: gateway metadata (provider, cache, fallbacks, truth-gate rejections); never text.
    ai_meta: dict[str, Any] = field(default_factory=dict)


class PreparationService:
    def __init__(
        self,
        db: Session,
        tenant_id: str,
        polisher: Optional[TextPolisher] = None,
        actor: str = DEFAULT_ACTOR,
    ):
        if not tenant_id:
            raise ValidationFailed("tenant_id is required")
        self.db = db
        self.tenant_id = tenant_id
        self.actor = actor
        self._polisher = polisher
        self._ctx: Optional[PreparationContext] = None
        self.repo = OpportunityRepository(db, tenant_id)
        self.evidence_repo = EvidenceRepository(db, tenant_id)

    # ------------------------------------------------------------------ #
    # context (loaded once per service instance)
    # ------------------------------------------------------------------ #

    def context(self, refresh: bool = False) -> PreparationContext:
        if self._ctx is None or refresh:
            snapshot = EvidenceSnapshot.load(self.evidence_repo)
            policy = PolicyRepository(self.db, self.tenant_id).get()
            self._ctx = PreparationContext(
                snapshot=snapshot,
                policy=policy,
                validator=PreparationValidator(snapshot),
                resolver=AnswerResolver(snapshot),
                polisher=self._polisher or get_polisher(self.tenant_id, policy.ai_settings),
            )
        return self._ctx

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #

    def _query(self):
        return self.db.query(ApplicationPreparationRow).filter(
            ApplicationPreparationRow.tenant_id == self.tenant_id
        )

    def get(self, preparation_id: str) -> Optional[ApplicationPreparationRow]:
        return self._query().filter(ApplicationPreparationRow.id == preparation_id).first()

    def require(self, preparation_id: str) -> ApplicationPreparationRow:
        row = self.get(preparation_id)
        if row is None:
            raise NotFoundError(f"Preparation not found: {preparation_id}")
        return row

    def latest(self, co_id: str) -> Optional[ApplicationPreparationRow]:
        return (
            self._query()
            .filter(ApplicationPreparationRow.candidate_opportunity_id == co_id)
            .order_by(ApplicationPreparationRow.version.desc())
            .first()
        )

    def list_for(self, co_id: str) -> list[ApplicationPreparationRow]:
        return (
            self._query()
            .filter(ApplicationPreparationRow.candidate_opportunity_id == co_id)
            .order_by(ApplicationPreparationRow.version.desc())
            .all()
        )

    def list_all(self, status: Optional[PreparationStatus] = None, limit: int = 100, offset: int = 0):
        query = self._query()
        if status is not None:
            query = query.filter(ApplicationPreparationRow.status == status.value)
        total = query.count()
        rows = query.order_by(ApplicationPreparationRow.updated_at.desc()).offset(offset).limit(limit).all()
        return rows, total

    def counts_by_status(self) -> dict[str, int]:
        from sqlalchemy import func

        rows = (
            self.db.query(ApplicationPreparationRow.status, func.count(ApplicationPreparationRow.id))
            .filter(ApplicationPreparationRow.tenant_id == self.tenant_id)
            .group_by(ApplicationPreparationRow.status)
            .all()
        )
        counts = {s.value: 0 for s in PreparationStatus}
        counts.update(dict(rows))
        return counts

    # ------------------------------------------------------------------ #
    # prepare
    # ------------------------------------------------------------------ #

    def prepare(
        self,
        co_id: str,
        level: Optional[TailoringLevel] = None,
        questions: Optional[list[str]] = None,
        force: bool = False,
        actor: Optional[str] = None,
    ) -> ApplicationPreparationRow:
        """Prepare one candidate opportunity; structured errors propagate."""
        actor = actor or self.actor
        ctx = self.context()
        targets = self._load_targets([co_id])
        if not targets:
            raise NotFoundError(f"Candidate opportunity not found: {co_id}")
        target = targets[0]
        previous = self._latest_map([co_id]).get(co_id)
        row, _created = self._prepare_target(ctx, target, previous, level, questions, force, actor)
        self.db.commit()
        return row

    def _prepare_target(
        self,
        ctx: PreparationContext,
        target: Target,
        previous: Optional[ApplicationPreparationRow],
        level: Optional[TailoringLevel],
        questions: Optional[list[str]],
        force: bool,
        actor: str,
    ) -> tuple[ApplicationPreparationRow, bool]:
        """Returns ``(row, created)``; ``created`` is False when reused."""
        questions = self._questions_for(previous, questions)
        blocked = self._admission_problem(target.co, force)
        if blocked:
            raise PolicyBlocked(blocked)
        chosen_level = level or self._level_for(ctx.policy, target.co)
        fingerprint, inputs = self._fingerprint(ctx, target, chosen_level, questions)
        if (
            previous is not None
            and not force
            and previous.input_fingerprint == fingerprint
            and previous.status not in (PreparationStatus.FAILED.value, PreparationStatus.INVALIDATED.value)
        ):
            return previous, False
        build = self._build(ctx, target, chosen_level, questions)
        row = self._persist(ctx, target, build, fingerprint, inputs, previous, actor)
        return row, True

    @staticmethod
    def _questions_for(previous: Optional[ApplicationPreparationRow], questions: Optional[list[str]]) -> list[str]:
        """The caller's questions; else the previous package's own questions when they were the form's; else the standard set.

        First real dry run (2026-09-14): a package prepared with the employer
        form's own questions (Notion asks no salary / notice period) was
        rebuilt by the queue worker with the standard set, which asked the
        candidate for facts the form never requests and kept it out of READY.
        """
        if questions is not None:
            return list(questions)
        if previous is not None and previous.inputs and "questions" in previous.inputs:
            standard = sorted(normalize_question(q) for q in DEFAULT_QUESTIONS)
            if list(previous.inputs["questions"]) != standard:
                return [answer.question for answer in sorted(previous.answers, key=lambda a: a.position)]
        return list(DEFAULT_QUESTIONS)

    def prepare_many(
        self,
        co_ids: list[str],
        level: Optional[TailoringLevel] = None,
        questions: Optional[list[str]] = None,
        force: bool = False,
        actor: Optional[str] = None,
        commit_every: int = 100,
    ) -> PrepareManyReport:
        actor = actor or self.actor
        ctx = self.context()
        report = PrepareManyReport(tenant_id=self.tenant_id, requested=len(co_ids))
        targets = self._load_targets(co_ids)
        latest_by_co = self._latest_map([t.co.id for t in targets])
        missing = set(co_ids) - {t.co.id for t in targets}
        for co_id in missing:
            report.errors.append(f"candidate opportunity not found: {co_id}")
            report.skipped += 1

        pending = 0
        for target in targets:
            try:
                previous = latest_by_co.get(target.co.id)
                row, created = self._prepare_target(ctx, target, previous, level, questions, force, actor)
                report.by_status[row.status] = report.by_status.get(row.status, 0) + 1
                if not created:
                    report.reused += 1
                    continue
                latest_by_co[target.co.id] = row
                report.created += 1
                report.ai_calls += row.ai_calls or 0
                pending += 1
                if pending >= commit_every:
                    self.db.commit()
                    pending = 0
            except PolicyBlocked as exc:
                report.skipped += 1
                report.errors.append(f"{target.co.id}: {exc.message}")
            except Exception as exc:  # noqa: BLE001 - one package must not stop the batch
                self.db.rollback()
                pending = 0
                logger.exception("Preparation failed for candidate opportunity %s", target.co.id)
                report.errors.append(f"{target.co.id}: {type(exc).__name__}: {exc}")
                report.skipped += 1
        self.db.commit()
        return report

    # ------------------------------------------------------------------ #

    def _load_targets(self, co_ids: list[str]) -> list[Target]:
        if not co_ids:
            return []
        cos = (
            self.db.query(CandidateOpportunityRow)
            .options(joinedload(CandidateOpportunityRow.opportunity))
            .filter(
                CandidateOpportunityRow.tenant_id == self.tenant_id,
                CandidateOpportunityRow.id.in_(list(co_ids)),
            )
            .all()
        )
        by_id = {co.id: co for co in cos}
        ordered = [by_id[c] for c in co_ids if c in by_id]
        job_ids = {co.opportunity.canonical_job_id for co in ordered if co.opportunity.canonical_job_id}
        jobs = {j.id: j for j in self.db.query(JobRow).filter(JobRow.id.in_(list(job_ids))).all()} if job_ids else {}
        match_ids = {co.match_id for co in ordered if co.match_id}
        matches = (
            {m.id: m for m in self.db.query(JobMatchRow).filter(JobMatchRow.id.in_(list(match_ids))).all()}
            if match_ids
            else {}
        )
        assessments: dict[str, list[RequirementAssessmentRow]] = {}
        if match_ids:
            for a in (
                self.db.query(RequirementAssessmentRow)
                .filter(RequirementAssessmentRow.job_match_id.in_(list(match_ids)))
                .all()
            ):
                assessments.setdefault(a.job_match_id, []).append(a)
        targets = []
        for co in ordered:
            match = matches.get(co.match_id) if co.match_id else None
            job = jobs.get(co.opportunity.canonical_job_id) if co.opportunity.canonical_job_id else None
            if job is None and match is not None:
                job = self.db.get(JobRow, match.job_id)
            targets.append(Target(co=co, job=job, match=match, assessments=assessments.get(co.match_id or "", [])))
        return targets

    def _latest_map(self, co_ids: list[str]) -> dict[str, ApplicationPreparationRow]:
        if not co_ids:
            return {}
        rows = (
            self._query()
            .filter(ApplicationPreparationRow.candidate_opportunity_id.in_(co_ids))
            .order_by(ApplicationPreparationRow.version.asc())
            .all()
        )
        latest: dict[str, ApplicationPreparationRow] = {}
        for row in rows:
            latest[row.candidate_opportunity_id] = row  # ascending order: last wins
        return latest

    @staticmethod
    def _admission_problem(co: CandidateOpportunityRow, force: bool) -> Optional[str]:
        state = OpportunityState(co.state)
        if state in (OpportunityState.INELIGIBLE, OpportunityState.CLOSED, OpportunityState.SKIPPED, OpportunityState.REJECTED):
            return f"candidate opportunity is {state.value}"
        if not force and not co.policy_admitted:
            return f"not admitted by the application policy: {co.policy_reason or 'not evaluated'}"
        return None

    @staticmethod
    def _level_for(policy: ApplicationPolicy, co: CandidateOpportunityRow) -> TailoringLevel:
        band = co.fit_band or ""
        return policy.tailoring_by_band.get(band, TailoringLevel.L0)

    @staticmethod
    def _cover_mode_for(policy: ApplicationPolicy, co: CandidateOpportunityRow) -> CoverLetterMode:
        band = co.fit_band or ""
        raw = (policy.cover_letter_by_band or {}).get(band, "DISABLED")
        try:
            return CoverLetterMode(raw)
        except ValueError:
            return CoverLetterMode.DISABLED

    def current_inputs(self, row: ApplicationPreparationRow) -> tuple[str, dict[str, Any]]:
        """What the fingerprint of ``row`` would be if it were prepared *now*.

        Used by the execution foundation to detect a stale package: a changed
        job description, evidence version, policy or variant produces a
        different digest. The stored normalised question keys are reused so
        only real inputs can differ.
        """
        ctx = self.context()
        targets = self._load_targets([row.candidate_opportunity_id])
        if not targets:
            raise NotFoundError(f"Candidate opportunity not found: {row.candidate_opportunity_id}")
        questions = list((row.inputs or {}).get("questions") or [a.question for a in row.answers])
        return self._fingerprint(ctx, targets[0], TailoringLevel(row.tailoring_level), questions)

    #: Inputs whose change alone does not make prepared *material* stale: the
    #: policy version is bumped by cap/blocklist edits too, and the parts of
    #: the policy that shape the package (lane, level, cover mode) are
    #: fingerprinted explicitly. ``match_id`` joined them on 2026-09-22 (see
    #: :meth:`_match_fingerprint`): it is kept for traceability only.
    _MATERIAL_NEUTRAL_INPUTS = frozenset({"policy_version", "match_id"})
    #: Input keys introduced after packages were already stored. Absent from a
    #: legacy ``row.inputs``, so their mere appearance is not a change; without
    #: this every preparation prepared before them would go stale exactly once.
    _ADDED_SINCE_V1 = frozenset({"match_fingerprint"})

    def stale_inputs(self, row: ApplicationPreparationRow) -> list[str]:
        """Names of the inputs that changed since ``row`` was prepared (empty = current).

        Compares the *inputs*, not the digest, so a policy edit that changed
        nothing the package depends on is not a reason to re-prepare.
        """
        digest, inputs = self.current_inputs(row)
        if digest == row.input_fingerprint:
            return []
        stored = row.inputs or {}
        added = set(inputs) - set(stored)
        changed = sorted(k for k in set(inputs) | set(stored) if inputs.get(k) != stored.get(k))
        material = [
            k
            for k in changed
            if k not in self._MATERIAL_NEUTRAL_INPUTS and not (k in added and k in self._ADDED_SINCE_V1)
        ]
        if not material and changed:
            return []
        return material or ["fingerprint"]

    @staticmethod
    def _match_fingerprint(target: Target) -> Optional[str]:
        """Digest of the parts of the match a package actually depends on.

        Autopilot finding (2026-09-22): a full match run mints a *new*
        ``JobMatchRow`` id for every job even when the assessment is identical,
        so fingerprinting ``match_id`` made every READY attempt STALE on the
        next cycle ("inputs changed since preparation: match_id") and bounced
        it back to PREPARING for ever. What the package reads from the match is
        the evidence :func:`select_evidence` selects and the requirement
        statuses ``_build`` reports, so that content is fingerprinted instead.
        """
        if target.match is None and not target.assessments:
            return None
        payload = {
            "evidence_references": sorted(k for a in target.assessments for k in (a.evidence_references or [])),
            "requirements": sorted([a.requirement_name or "", a.status or ""] for a in target.assessments),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def _fingerprint(
        self, ctx: PreparationContext, target: Target, level: TailoringLevel, questions: list[str]
    ) -> tuple[str, dict[str, Any]]:
        matched_keys = [k for a in target.assessments for k in (a.evidence_references or [])]
        choice = select_variant(ctx.snapshot.variants, target.job.title if target.job else target.co.opportunity.title, matched_keys)
        inputs = {
            "evidence_fingerprint": ctx.snapshot.fingerprint,
            "job_id": target.job.id if target.job else None,
            "job_content_hash": target.job.content_hash if target.job else None,
            "match_id": target.match.id if target.match else None,  # traceability only; see _MATERIAL_NEUTRAL_INPUTS
            "match_fingerprint": self._match_fingerprint(target),
            "tailoring_level": level.value,
            "lane": ctx.policy.lane_by_band.get(target.co.fit_band or "", Lane.REVIEW).value,
            "cover_letter_mode": self._cover_mode_for(ctx.policy, target.co).value,
            "positioning_variant_id": choice.variant.id if choice.variant else None,
            "positioning_variant_version": choice.variant.version if choice.variant else None,
            "policy_version": ctx.policy.version,
            "template_version": TEMPLATE_VERSION,
            "questions": sorted(normalize_question(q) for q in questions),
            "ai": ctx.polisher.name if level is TailoringLevel.L2 else None,
        }
        digest = hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        return digest, inputs

    def _build(self, ctx: PreparationContext, target: Target, level: TailoringLevel, questions: list[str]) -> Build:
        snapshot = ctx.snapshot
        job_title = target.job.title if target.job else target.co.opportunity.title
        company = target.job.company if target.job else target.co.opportunity.company
        matched_keys = [k for a in target.assessments for k in (a.evidence_references or [])]
        choice = select_variant(snapshot.variants, job_title, matched_keys)
        selection = select_evidence(snapshot, target.assessments, choice.variant, level)
        lane = ctx.policy.lane_by_band.get(target.co.fit_band or "", Lane.REVIEW)
        cover_mode = self._cover_mode_for(ctx.policy, target.co)

        resume_blocks = compose_resume(snapshot, selection, choice.variant, level, job_title)
        cover_blocks = compose_cover_letter(cover_mode, snapshot, selection, choice.variant, company, job_title)
        # Job facts and candidate-authored framing may appear inside claims
        # without being candidate evidence (see PreparationValidator.validate_block).
        # Scanned once here, reused for every block and answer of this package.
        context = ctx.validator.make_context(self._context_text(target, choice.variant, job_title, company))

        ai_calls = 0
        ai_used = False
        ai_meta: dict[str, Any] = {"used": False, "level": level.value}
        if level is TailoringLevel.L2 and not isinstance(ctx.polisher, StubPolisher):
            reset = getattr(ctx.polisher, "reset_budget", None)
            if callable(reset):
                reset()  # one call budget per package
            self._truth_rejections = 0
            resume_blocks, calls, used = self._polish(ctx, resume_blocks, "resume bullet", context)
            ai_calls += calls
            ai_used = ai_used or used
            if cover_blocks and cover_mode is CoverLetterMode.TARGETED:
                cover_blocks, calls, used = self._polish(ctx, cover_blocks, "cover letter sentence", context)
                ai_calls += calls
                ai_used = ai_used or used
            describe = getattr(ctx.polisher, "metadata", None)
            ai_meta = dict(describe()) if callable(describe) else {"provider": ctx.polisher.name, "model": getattr(ctx.polisher, "model", None), "calls": ai_calls}
            ai_meta.update({"used": ai_used, "level": level.value, "offered": ai_calls, "rejected_by_truth_gate": self._truth_rejections})

        all_blocks = resume_blocks + (cover_blocks or [])
        report = ctx.validator.validate_blocks(all_blocks, context)
        lossy = False
        if not report.passed:
            resume_blocks, resume_report = ctx.validator.drop_failing(resume_blocks, context)
            if cover_blocks is not None:
                cover_blocks, cover_report = ctx.validator.drop_failing(cover_blocks, context)
                issues = resume_report.issues + cover_report.issues
                dropped = resume_report.dropped_blocks + cover_report.dropped_blocks
            else:
                issues, dropped = resume_report.issues, resume_report.dropped_blocks
            report = ValidationReport(passed=False, checked_blocks=resume_report.checked_blocks, issues=issues, dropped_blocks=dropped)
            lossy = True

        answers = self._resolve_answers(ctx, questions, selection, job_title, company, context)

        has_claims = any(b.kind is BlockKind.CLAIM for b in resume_blocks)
        validation_status = ValidationStatus.PASSED if has_claims or not lossy else ValidationStatus.FAILED
        if not has_claims:
            validation_status = ValidationStatus.FAILED
        if validation_status is ValidationStatus.FAILED:
            status = PreparationStatus.NEEDS_REVIEW
        elif any(a["status"] == PreparedAnswerStatus.NEEDS_USER_INPUT.value and a["required"] for a in answers):
            status = PreparationStatus.NEEDS_USER_INPUT
        elif lossy or any(a["status"] == PreparedAnswerStatus.NEEDS_REVIEW.value for a in answers):
            status = PreparationStatus.NEEDS_REVIEW
        else:
            status = PreparationStatus.READY

        return Build(
            level=level,
            lane=lane,
            cover_mode=cover_mode,
            choice=choice,
            selection=selection,
            resume_blocks=resume_blocks,
            cover_blocks=cover_blocks,
            answers=answers,
            report=report,
            validation_status=validation_status,
            status=status,
            ai_calls=ai_calls,
            ai_used=ai_used,
            ai_meta=ai_meta,
        )

    @staticmethod
    def _context_text(target: Target, variant: Optional[PositioningVariantRow], job_title: str, company: str) -> str:
        parts = [job_title, company]
        parts.extend(a.requirement_name or "" for a in target.assessments)
        if variant is not None:
            parts.extend([variant.headline or "", variant.summary or ""])
        return "\n".join(p for p in parts if p)

    def _polish(
        self, ctx: PreparationContext, blocks: list[Block], purpose: str, context: Any = None
    ) -> tuple[list[Block], int, bool]:
        polished: list[Block] = []
        calls = 0
        used = False
        offered = 0
        for index, block in enumerate(blocks):
            if block.kind is not BlockKind.CLAIM or offered >= L2_POLISH_BLOCKS:
                polished.append(block)
                continue
            offered += 1
            calls += 1
            try:
                rewritten = ctx.polisher.polish(block.text, ctx.snapshot.supporting_text(block.evidence_keys), purpose)
            except Exception as exc:  # noqa: BLE001 - a polisher failure never fails the package
                logger.warning("AI polish raised %s; keeping deterministic text", type(exc).__name__)
                rewritten = None
            if not rewritten or rewritten.strip() == block.text.strip():
                polished.append(block)
                continue
            candidate = block.model_copy(update={"text": rewritten.strip(), "ai_polished": True, "source": f"ai:{ctx.polisher.name}"})
            if ctx.validator.validate_block(candidate, index, context):
                # Rewrite rejected by the truth gate (unsupported claim, number,
                # employer, skill...): keep the deterministic original. AI never
                # becomes evidence.
                self._truth_rejections = getattr(self, "_truth_rejections", 0) + 1
                polished.append(block)
            else:
                polished.append(candidate)
                used = True
        return polished, calls, used

    def _resolve_answers(
        self,
        ctx: PreparationContext,
        questions: list[str],
        selection: EvidenceSelection,
        job_title: str,
        company: str,
        context: Any = None,
    ) -> list[dict[str, Any]]:
        answers: list[dict[str, Any]] = []
        seen: set[str] = set()
        profile_statement = ctx.snapshot.profile.positioning_statement if ctx.snapshot.profile else ""
        base_text = context.text if context is not None and hasattr(context, "text") else (context or "")
        answer_context = ctx.validator.make_context(f"{base_text}\n{profile_statement}")
        for position, question in enumerate(questions):
            key = normalize_question(question)
            if not key or key in seen:
                continue
            seen.add(key)
            resolved = ctx.resolver.resolve(question, selection, job_title, company)
            status = resolved.status
            reason = resolved.reason
            if resolved.source is AnswerSource.GENERATED and resolved.answer:
                block = Block(kind=BlockKind.CLAIM, section="answer", text=resolved.answer, evidence_keys=resolved.evidence_keys)
                issues = ctx.validator.validate_block(block, None, answer_context)
                if issues:
                    status = PreparedAnswerStatus.NEEDS_REVIEW
                    reason = "generated answer failed validation: " + "; ".join(i.code for i in issues)
            answers.append(
                {
                    "position": position,
                    "question": question[:512],
                    "question_key": key,
                    "category": resolved.category.value,
                    "answer": resolved.answer if status is PreparedAnswerStatus.ANSWERED else None,
                    "evidence_keys": list(resolved.evidence_keys),
                    "source": resolved.source.value if status is PreparedAnswerStatus.ANSWERED else AnswerSource.NONE.value,
                    "status": status.value,
                    "required": resolved.category is not QuestionCategory.VOLUNTARY,
                    "answer_bank_entry_id": resolved.entry_id,
                    "reason": (reason or "")[:256] or None,
                }
            )
        return answers

    def _persist(
        self,
        ctx: PreparationContext,
        target: Target,
        build: Build,
        fingerprint: str,
        inputs: dict[str, Any],
        previous: Optional[ApplicationPreparationRow],
        actor: str,
    ) -> ApplicationPreparationRow:
        version = (previous.version + 1) if previous else 1
        if previous is not None and previous.status not in (
            PreparationStatus.SUPERSEDED.value,
            PreparationStatus.INVALIDATED.value,
        ):
            before = {"status": previous.status, "version": previous.version}
            previous.status = PreparationStatus.SUPERSEDED.value
            previous.updated_at = db_now()
            self.repo.record("preparation", previous.id, "superseded", actor, before, {"status": previous.status, "by_version": version})

        evidence_keys = build.selection.all_keys
        for block in build.resume_blocks + (build.cover_blocks or []):
            for key in block.evidence_keys:
                if key not in evidence_keys:
                    evidence_keys.append(key)

        row = ApplicationPreparationRow(
            id=new_id(),
            tenant_id=self.tenant_id,
            candidate_opportunity_id=target.co.id,
            opportunity_id=target.co.opportunity_id,
            job_id=target.job.id if target.job else None,
            job_content_hash=target.job.content_hash if target.job else None,
            version=version,
            tailoring_level=build.level.value,
            lane=build.lane.value,
            cover_letter_mode=build.cover_mode.value,
            positioning_variant_id=build.choice.variant.id if build.choice.variant else None,
            positioning_variant_version=build.choice.variant.version if build.choice.variant else None,
            positioning_reason=build.choice.reason[:256],
            status=build.status.value,
            validation_status=build.validation_status.value,
            validation_report=build.report.model_dump(),
            input_fingerprint=fingerprint,
            inputs=inputs,
            evidence_keys=evidence_keys,
            ai_used=build.ai_used,
            ai_provider=ctx.polisher.name if build.ai_used else None,
            ai_model=getattr(ctx.polisher, "model", None) if build.ai_used else None,
            ai_calls=build.ai_calls,
        )
        self.db.add(row)
        self.db.add(
            PreparationArtifactRow(
                tenant_id=self.tenant_id,
                preparation_id=row.id,
                artifact_type=ArtifactKind.RESUME.value,
                content=render(build.resume_blocks),
                blocks=[b.model_dump() for b in build.resume_blocks],
                evidence_keys=sorted({k for b in build.resume_blocks for k in b.evidence_keys}),
                template_version=TEMPLATE_VERSION,
                ai_used=any(b.ai_polished for b in build.resume_blocks),
                validation_status=build.validation_status.value,
            )
        )
        if build.cover_blocks is not None:
            self.db.add(
                PreparationArtifactRow(
                    tenant_id=self.tenant_id,
                    preparation_id=row.id,
                    artifact_type=ArtifactKind.COVER_LETTER.value,
                    content=render(build.cover_blocks),
                    blocks=[b.model_dump() for b in build.cover_blocks],
                    evidence_keys=sorted({k for b in build.cover_blocks for k in b.evidence_keys}),
                    template_version=TEMPLATE_VERSION,
                    ai_used=any(b.ai_polished for b in build.cover_blocks),
                    validation_status=build.validation_status.value,
                )
            )
        for answer in build.answers:
            self.db.add(PreparationAnswerRow(tenant_id=self.tenant_id, preparation_id=row.id, **answer))

        # Audit: one event per lifecycle fact, never the generated text itself.
        counts_by_source: dict[str, int] = {}
        counts_by_status: dict[str, int] = {}
        for answer in build.answers:
            counts_by_source[answer["source"]] = counts_by_source.get(answer["source"], 0) + 1
            counts_by_status[answer["status"]] = counts_by_status.get(answer["status"], 0) + 1
        self.repo.record(
            "preparation",
            row.id,
            "created" if previous is None else "regenerated",
            actor,
            None,
            {
                "version": version,
                "tailoring_level": build.level.value,
                "lane": build.lane.value,
                "cover_letter_mode": build.cover_mode.value,
                "positioning_variant_id": row.positioning_variant_id,
                "positioning_reason": build.choice.reason,
                "evidence_selected": len(evidence_keys),
                "evidence_excluded": build.selection.excluded,
                "matched_requirements": list(build.selection.matched_requirements.keys())[:20],
                "answers_by_source": counts_by_source,
                "answers_by_status": counts_by_status,
                "ai_calls": build.ai_calls,
                "ai_used": build.ai_used,
                "ai": build.ai_meta,
            },
            build.choice.reason,
        )
        self.repo.record(
            "preparation",
            row.id,
            "validation_passed" if build.validation_status is ValidationStatus.PASSED else "validation_failed",
            actor,
            None,
            build.report.model_dump(),
        )
        if build.status is PreparationStatus.NEEDS_USER_INPUT:
            self.repo.record(
                "preparation",
                row.id,
                "user_input_required",
                actor,
                None,
                {"questions": [a["question"] for a in build.answers if a["status"] == PreparedAnswerStatus.NEEDS_USER_INPUT.value]},
            )
        self._move_candidate(target.co, build.status, actor)
        self.db.flush()
        return row

    def _move_candidate(self, co: CandidateOpportunityRow, status: PreparationStatus, actor: str) -> None:
        state = OpportunityState(co.state)
        if status is PreparationStatus.READY and state in _PREPARED_FROM:
            self.repo.transition(co, OpportunityState.PREPARED, actor, "preparation ready")
        elif status in (PreparationStatus.NEEDS_USER_INPUT, PreparationStatus.NEEDS_REVIEW) and state in _REVIEW_FROM:
            self.repo.transition(co, OpportunityState.IN_REVIEW, actor, f"preparation {status.value.lower()}")

    # ------------------------------------------------------------------ #
    # review actions
    # ------------------------------------------------------------------ #

    def approve(self, preparation_id: str, actor: str) -> ApplicationPreparationRow:
        row = self.require(preparation_id)
        if row.status not in (PreparationStatus.NEEDS_REVIEW.value, PreparationStatus.READY.value):
            raise ConflictError(f"Cannot approve a {row.status} preparation")
        if any(a.status == PreparedAnswerStatus.NEEDS_USER_INPUT.value and a.required for a in row.answers):
            raise ConflictError("Answer the required questions before approving")
        before = {"status": row.status}
        row.status = PreparationStatus.READY.value
        row.approved_at = db_now()
        row.approved_by = actor
        row.updated_at = db_now()
        for answer in row.answers:
            if answer.status == PreparedAnswerStatus.NEEDS_REVIEW.value and answer.answer:
                answer.status = PreparedAnswerStatus.ANSWERED.value
        self.repo.record("preparation", row.id, "approved", actor, before, {"status": row.status})
        co = self.repo.get_candidate_opportunity(row.candidate_opportunity_id)
        if co is not None:
            self._move_candidate(co, PreparationStatus.READY, actor)
        self.db.flush()
        return row

    def reject(self, preparation_id: str, actor: str, reason: Optional[str] = None) -> ApplicationPreparationRow:
        row = self.require(preparation_id)
        before = {"status": row.status}
        row.status = PreparationStatus.NEEDS_REVIEW.value
        row.updated_at = db_now()
        self.repo.record("preparation", row.id, "rejected", actor, before, {"status": row.status}, reason)
        self.db.flush()
        return row

    def invalidate(self, preparation_id: str, actor: str, reason: Optional[str] = None) -> ApplicationPreparationRow:
        row = self.require(preparation_id)
        before = {"status": row.status}
        row.status = PreparationStatus.INVALIDATED.value
        row.updated_at = db_now()
        self.repo.record("preparation", row.id, "invalidated", actor, before, {"status": row.status}, reason)
        self.db.flush()
        return row

    def answer_question(
        self,
        preparation_id: str,
        answer_id: str,
        text: str,
        actor: str,
        save_to_bank: bool = False,
        category: Optional[str] = None,
    ) -> ApplicationPreparationRow:
        """Record the candidate's own answer; optionally store it as a reusable draft."""
        row = self.require(preparation_id)
        answer = next((a for a in row.answers if a.id == answer_id), None)
        if answer is None:
            raise NotFoundError(f"Answer not found on this preparation: {answer_id}")
        text = (text or "").strip()
        if not text:
            raise ValidationFailed("Answer cannot be empty")
        before = {"status": answer.status, "source": answer.source}
        answer.answer = text
        answer.source = AnswerSource.USER.value
        answer.status = PreparedAnswerStatus.ANSWERED.value
        answer.reason = "answered by the candidate"
        answer.updated_at = db_now()
        if save_to_bank:
            entry = self.evidence_repo.find_answer(answer.question)
            if entry is None:
                created = self.evidence_repo.create_answer(
                    AnswerBankEntryCreate(
                        category=category or answer.category,
                        question=answer.question,
                        answer=text,
                        evidence_keys=[],
                        status=AnswerStatus.APPROVED,
                    ),
                    actor,
                )
                answer.answer_bank_entry_id = created.id
        self.repo.record("preparation", row.id, "answer_changed", actor, before, {"status": answer.status, "source": answer.source, "question": answer.question[:120]})

        if row.status in (PreparationStatus.NEEDS_USER_INPUT.value, PreparationStatus.NEEDS_REVIEW.value):
            still_missing = any(a.status == PreparedAnswerStatus.NEEDS_USER_INPUT.value and a.required for a in row.answers)
            needs_review = any(a.status == PreparedAnswerStatus.NEEDS_REVIEW.value for a in row.answers)
            new_status = (
                PreparationStatus.NEEDS_USER_INPUT
                if still_missing
                else PreparationStatus.NEEDS_REVIEW
                if needs_review or row.validation_status != ValidationStatus.PASSED.value
                else PreparationStatus.READY
            )
            if new_status.value != row.status:
                before_status = {"status": row.status}
                row.status = new_status.value
                row.updated_at = db_now()
                self.repo.record("preparation", row.id, f"status:{new_status.value}", actor, before_status, {"status": row.status})
                co = self.repo.get_candidate_opportunity(row.candidate_opportunity_id)
                if co is not None:
                    self._move_candidate(co, new_status, actor)
        self.db.flush()
        return row

    # ------------------------------------------------------------------ #

    @staticmethod
    def variant_label(variant: Optional[PositioningVariantRow]) -> str:
        return f"{variant.role_family} / {variant.name}" if variant else "profile statement"
