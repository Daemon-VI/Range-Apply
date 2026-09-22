"""The Signal Inbox service (Blueprint Phase 10).

    connector / execution / person
        │  SignalIngest (or EmailMessage)
        ▼
    ingest()      idempotent per tenant on a stable reference or the content hash;
                  repeats become observations, never a second signal
        ▼
    process()     classify (rules; optional AI through the gateway; a declared
                  category for MANUAL) → attribute (strong identifiers first,
                  company/title last, AMBIGUOUS/UNMATCHED instead of a guess)
                  → record an outcome event with an evidence strength
                  → re-derive the application's current status
                  → guarded lifecycle sync (INTERVIEWING / REJECTED only, never forced)
                  → confirmation evidence for a SUBMITTED / UNCERTAIN attempt goes
                    through ``ExecutionService.verify`` (the one verification path)
        ▼
    review        link / confirm / reject / ignore / merge / reprocess — every
                  decision is a new attribution or event row plus an audit event;
                  earlier rows are superseded or retracted, never deleted.

Nothing here writes to the Evidence Graph. External evidence can establish
that an event happened; it never establishes a candidate fact.
"""

import logging
from collections import Counter
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.application.database.models import ApplicationEventRow, ApplicationRow
from app.application.models import ApplicationStatus
from app.config import settings
from app.core.errors import CareerOSError, ConflictError, NotFoundError, ValidationFailed
from app.core.timeutils import db_now
from app.execution.database.models import ExecutionRunRow
from app.execution.models import (
    ExecutionStatus,
    VerificationMethod,
    VerificationResult,
    VerificationStatus,
)
from app.pipeline.database.models import CandidateOpportunityRow, OpportunityRow
from app.pipeline.models import OpportunityState
from app.pipeline.repository import OpportunityRepository, PolicyRepository
from app.scheduler.attempts import AttemptRepository
from app.signals.ai import GatewaySignalClassifier, get_signal_classifier
from app.signals.attribution import AttemptIndex, Attributor
from app.signals.classify import classify_text
from app.signals.database.models import (
    ApplicationOutcomeRow,
    OutcomeEventRow,
    SignalAttributionRow,
    SignalObservationRow,
    SignalRow,
)
from app.signals.models import (
    ATTRIBUTION_VERSION,
    CLASSIFIER_VERSION,
    OUTCOME_RULES_VERSION,
    SIGNAL_SCHEMA_VERSION,
    ApplicationOutcome,
    AttributionHints,
    AttributionResult,
    AttributionStatus,
    Classification,
    ClassificationSource,
    Confidence,
    DerivedOutcome,
    EmailMessage,
    EventOrigin,
    EvidenceStrength,
    OutcomeEvent,
    OutcomeKind,
    Signal,
    SignalAttribution,
    SignalCategory,
    SignalIngest,
    SignalObservation,
    SignalSource,
    SignalStatus,
    SignalSummary,
    SignalTrace,
    TimeBasis,
)
from app.signals.normalize import NormalizedSignal, email_to_ingest
from app.signals.outcomes import (
    OUTCOME_FOR_CATEGORY,
    derive,
    evidence_for,
    outcome_for_verification,
)

logger = logging.getLogger(__name__)

DEFAULT_ACTOR = "signals"
_REVIEW_STATUSES = (SignalStatus.NEEDS_REVIEW.value, SignalStatus.UNMATCHED.value)
_REPROCESSABLE = (SignalStatus.NEW.value, SignalStatus.NEEDS_REVIEW.value, SignalStatus.UNMATCHED.value, SignalStatus.ATTRIBUTED.value, SignalStatus.APPLIED.value)
_CONFIRMATION_CATEGORIES = (SignalCategory.APPLICATION_CONFIRMATION, SignalCategory.APPLICATION_RECEIVED)


class SignalInboxService:
    def __init__(self, db: Session, tenant_id: str, actor: str = DEFAULT_ACTOR, classifier: Optional[GatewaySignalClassifier] = None, index: Optional[AttemptIndex] = None):
        if not tenant_id:
            raise ValidationFailed("tenant_id is required")
        self.db = db
        self.tenant_id = tenant_id
        self.actor = actor
        self.repo = OpportunityRepository(db, tenant_id)
        self.attempts = AttemptRepository(db, tenant_id)
        self.attributor = Attributor(db, tenant_id, index)
        self._ai = classifier
        self._ai_resolved = classifier is not None
        self._policy = None
        self.counts: Counter = Counter()

    # ------------------------------------------------------------------ #
    # ingestion (the connector boundary)
    # ------------------------------------------------------------------ #

    def ingest(self, request: SignalIngest, process: Optional[bool] = None) -> tuple[SignalRow, bool]:
        """Store one observation. Returns ``(signal, created)``; a repeat of a
        known signal returns ``created=False`` and adds an observation."""
        normalized = NormalizedSignal(request, excerpt_chars=settings.signal_excerpt_chars)
        if request.category is not None and request.source is not SignalSource.MANUAL:
            raise ValidationFailed("only MANUAL signals may declare a category")
        existing = self._find_dedupe(normalized.dedupe_key)
        if existing is not None:
            self._observe(existing, normalized)
            self.counts["duplicates"] += 1
            self._audit(existing, "observed", {"observation_count": existing.observation_count, "content_hash": normalized.content_hash})
            self.db.flush()
            return existing, False
        row = SignalRow(
            tenant_id=self.tenant_id,
            source=normalized.source.value,
            source_reference=normalized.source_reference,
            content_hash=normalized.content_hash,
            dedupe_key=normalized.dedupe_key,
            subject=normalized.subject,
            sender=normalized.sender,
            sender_domain=normalized.sender_domain,
            excerpt=normalized.excerpt,
            payload={**normalized.payload, "hints": normalized.hints.model_dump(exclude_none=True), "references": list(normalized.references), "urls": list(normalized.urls[:5])},
            external_at=normalized.external_at,
            observed_at=normalized.observed_at,
            observation_count=1,
            last_observed_at=normalized.observed_at,
            execution_run_id=normalized.hints.execution_run_id,
            classification={"declared": request.category.value} if request.category else {},
            schema_version=SIGNAL_SCHEMA_VERSION,
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError:
            existing = self._find_dedupe(normalized.dedupe_key)
            if existing is None:
                raise
            self._observe(existing, normalized)
            self.counts["duplicates"] += 1
            self.db.flush()
            return existing, False
        self.db.add(SignalObservationRow(tenant_id=self.tenant_id, signal_id=row.id, source=row.source, source_reference=row.source_reference, content_hash=row.content_hash, observed_at=normalized.observed_at, provenance=normalized.provenance))
        self.counts["ingested"] += 1
        self._audit(row, "ingested", {"source": row.source, "reference": row.source_reference, "content_hash": row.content_hash})
        if process if process is not None else request.process:
            self.process(row, normalized)
        self.db.flush()
        return row, True

    def ingest_email(self, message: EmailMessage, process: Optional[bool] = None) -> tuple[SignalRow, bool]:
        return self.ingest(email_to_ingest(message), process=process)

    def ingest_many(self, requests: list[SignalIngest]) -> dict[str, int]:
        before = Counter(self.counts)
        for request in requests:
            self.ingest(request)
        return {k: self.counts[k] - before.get(k, 0) for k in ("ingested", "duplicates", "processed", "applied", "needs_review", "unmatched", "ignored")}

    def _find_dedupe(self, key: str) -> Optional[SignalRow]:
        return self.db.query(SignalRow).filter(SignalRow.tenant_id == self.tenant_id, SignalRow.dedupe_key == key).first()

    def _observe(self, row: SignalRow, normalized: NormalizedSignal) -> None:
        # Atomic increment (Phase 12): two parallel deliveries must not lose an
        # observation to a read-modify-write race.
        self.db.query(SignalRow).filter(SignalRow.id == row.id).update({SignalRow.observation_count: SignalRow.observation_count + 1, SignalRow.last_observed_at: normalized.observed_at}, synchronize_session=False)
        self.db.expire(row, ["observation_count", "last_observed_at"])
        provenance = dict(normalized.provenance)
        if normalized.content_hash != row.content_hash:
            provenance["content_changed"] = True
        self.db.add(SignalObservationRow(tenant_id=self.tenant_id, signal_id=row.id, source=normalized.source.value, source_reference=normalized.source_reference, content_hash=normalized.content_hash, observed_at=normalized.observed_at, provenance=provenance))

    # ------------------------------------------------------------------ #
    # processing
    # ------------------------------------------------------------------ #

    def process(self, row: SignalRow, normalized: Optional[NormalizedSignal] = None) -> SignalRow:
        if row.tenant_id != self.tenant_id:
            raise NotFoundError("Signal not found in this tenant")
        if row.status in (SignalStatus.IGNORED.value, SignalStatus.MERGED.value):
            return row
        normalized = normalized or self._renormalize(row)
        classification = self._classify(row, normalized)
        self.counts["processed"] += 1
        if classification.category is SignalCategory.OTHER and classification.confident and row.classification_source != ClassificationSource.HUMAN.value:
            self._set_status(row, SignalStatus.IGNORED, "classified OTHER (alerts / marketing); not an application signal")
            row.attribution_status = AttributionStatus.SKIPPED.value
            self.counts["ignored"] += 1
            self._audit(row, "processed", {"category": row.category, "confidence": row.confidence, "attribution": row.attribution_status})
            return row
        attribution = self._attribute(row, normalized)
        self._apply(row, classification, attribution)
        self._audit(row, "processed", {"category": row.category, "confidence": row.confidence, "classification_source": row.classification_source, "attribution": row.attribution_status, "rule": attribution.rule, "application_id": row.application_id})
        return row

    def _renormalize(self, row: SignalRow) -> NormalizedSignal:
        payload = dict(row.payload or {})
        hints = AttributionHints(**(payload.pop("hints", None) or {}))
        if row.execution_run_id and not hints.execution_run_id:
            hints.execution_run_id = row.execution_run_id
        request = SignalIngest(source=SignalSource(row.source), source_reference=row.source_reference, subject=row.subject, text=row.excerpt, sender=row.sender, external_at=row.external_at, observed_at=row.observed_at, payload=payload, hints=hints, process=False)
        return NormalizedSignal(request, excerpt_chars=settings.signal_excerpt_chars)

    # --------------------------------------------------------- classify

    def _classify(self, row: SignalRow, normalized: NormalizedSignal) -> Classification:
        if row.classification_source == ClassificationSource.HUMAN.value:
            return Classification(category=SignalCategory(row.category), confidence=Confidence(row.confidence), source=ClassificationSource.HUMAN, classifier_version=row.classifier_version or CLASSIFIER_VERSION)
        payload = row.payload or {}
        declared = (row.classification or {}).get("declared")
        if row.source == SignalSource.MANUAL.value and declared:
            classification = Classification(category=SignalCategory(declared), confidence=Confidence.HIGH, source=ClassificationSource.DECLARED)
        elif payload.get("kind") == "execution_result":
            _, _, confidence = outcome_for_verification(payload.get("verification_status") or VerificationStatus.NOT_ATTEMPTED.value, payload.get("run_outcome"))
            classification = Classification(category=SignalCategory.EXECUTION_RESULT, confidence=confidence, source=ClassificationSource.EXECUTION, matched_rules=[f"verification:{payload.get('verification_status')}"])
        else:
            classification = classify_text(normalized.subject, normalized.text, marketing_sender=normalized.marketing)
            if not classification.confident and classification.category is not SignalCategory.OTHER:
                ai = self._ai_classifier()
                if ai is not None:
                    ai_result, meta = ai.classify(normalized.subject, normalized.excerpt, normalized.sender_domain, reference=f"signal:{row.id}")
                    row.ai = meta
                    if ai_result is not None:
                        ai_result.ai = {**(ai_result.ai or {}), "rules": {"category": classification.category.value, "confidence": classification.confidence.value, "matched_rules": classification.matched_rules}}
                        classification = ai_result
        row.category = classification.category.value
        row.confidence = classification.confidence.value
        row.classification_source = classification.source.value
        row.classifier_version = classification.classifier_version
        row.classification = {**{k: v for k, v in (row.classification or {}).items() if k == "declared"}, "matched_rules": classification.matched_rules, "competing": classification.competing, **({"ai": classification.ai} if classification.ai else {})}
        return classification

    def _ai_classifier(self) -> Optional[GatewaySignalClassifier]:
        if not self._ai_resolved:
            self._ai_resolved = True
            try:
                self._ai = get_signal_classifier(self.tenant_id, self.policy.ai_settings)
            except CareerOSError:
                self._ai = None
        return self._ai

    @property
    def policy(self):
        if self._policy is None:
            self._policy = PolicyRepository(self.db, self.tenant_id).get()
        return self._policy

    def ai_metadata(self) -> dict[str, Any]:
        return self._ai.metadata() if self._ai is not None else {"used": False, "calls": 0}

    # -------------------------------------------------------- attribute

    def _attribute(self, row: SignalRow, normalized: NormalizedSignal) -> AttributionResult:
        current = self.db.get(SignalAttributionRow, row.attribution_id) if row.attribution_id else None
        if current is not None and current.status == AttributionStatus.MANUAL.value:
            return AttributionResult(status=AttributionStatus.MANUAL, application_id=current.application_id, opportunity_id=current.opportunity_id, candidate_opportunity_id=current.candidate_opportunity_id, rule=current.rule, confidence=Confidence(current.confidence), evidence=dict(current.evidence or {}), attribution_version=current.attribution_version, explanation=current.explanation or "")
        result = self.attributor.attribute(normalized)
        self._store_attribution(row, result, self.actor, current)
        return result

    def _store_attribution(self, row: SignalRow, result: AttributionResult, actor: str, current: Optional[SignalAttributionRow]) -> SignalAttributionRow:
        if current is not None and current.status == result.status.value and current.application_id == result.application_id and current.rule == result.rule and current.attribution_version == result.attribution_version:
            return current  # identical decision: nothing to append
        new = SignalAttributionRow(
            tenant_id=self.tenant_id,
            signal_id=row.id,
            status=result.status.value,
            application_id=result.application_id,
            opportunity_id=result.opportunity_id,
            candidate_opportunity_id=result.candidate_opportunity_id,
            rule=result.rule,
            confidence=result.confidence.value,
            evidence=result.evidence,
            candidates=list(result.candidates),
            explanation=(result.explanation or "")[:512] or None,
            attribution_version=result.attribution_version,
            actor=actor,
        )
        self.db.add(new)
        self.db.flush()
        if current is not None:
            current.superseded_by_id = new.id
        row.attribution_id = new.id
        row.attribution_status = result.status.value
        row.application_id = result.application_id
        row.opportunity_id = result.opportunity_id
        row.candidate_opportunity_id = result.candidate_opportunity_id
        self._audit(row, "attributed", {"status": result.status.value, "rule": result.rule, "application_id": result.application_id, "candidates": list(result.candidates), "explanation": result.explanation, "version": result.attribution_version})
        return new

    # ------------------------------------------------------------ apply

    def _apply(self, row: SignalRow, classification: Classification, attribution: AttributionResult) -> None:
        if classification.category is SignalCategory.UNKNOWN:
            # Unclassified: a person decides, whatever the attribution said (it is kept).
            self._set_status(row, SignalStatus.NEEDS_REVIEW, f"unclassified; attribution {attribution.status.value.lower()} ({attribution.rule})")
            self.counts["needs_review"] += 1
            return
        if attribution.status is AttributionStatus.AMBIGUOUS:
            self._set_status(row, SignalStatus.NEEDS_REVIEW, attribution.explanation or "ambiguous attribution")
            self.counts["needs_review"] += 1
            return
        if attribution.status is AttributionStatus.UNMATCHED or not attribution.application_id:
            self._set_status(row, SignalStatus.UNMATCHED, attribution.explanation or "no application matched")
            self.counts["unmatched"] += 1
            return
        attempt = self.db.get(ApplicationRow, attribution.application_id)
        if attempt is None or attempt.tenant_id != self.tenant_id:
            self._set_status(row, SignalStatus.UNMATCHED, "the attributed application no longer exists")
            return
        payload = row.payload or {}
        if payload.get("kind") == "execution_result":
            outcome, evidence, _ = outcome_for_verification(payload.get("verification_status") or VerificationStatus.NOT_ATTEMPTED.value, payload.get("run_outcome"))
            origin = EventOrigin.EXECUTION
        else:
            outcome = OUTCOME_FOR_CATEGORY.get(classification.category)
            evidence = evidence_for(classification.confidence, classification.source)
            origin = {ClassificationSource.AI: EventOrigin.AI, ClassificationSource.HUMAN: EventOrigin.HUMAN, ClassificationSource.DECLARED: EventOrigin.HUMAN}.get(classification.source, EventOrigin.RULES)
        if outcome is None:
            reason = "no outcome follows from this category" if classification.category is not SignalCategory.UNKNOWN else "unclassified; a person must decide"
            self._set_status(row, SignalStatus.NEEDS_REVIEW if classification.category is SignalCategory.UNKNOWN else SignalStatus.ATTRIBUTED, reason)
            if classification.category is SignalCategory.UNKNOWN:
                self.counts["needs_review"] += 1
            return
        event, _ = self._record_event(attempt, row, outcome, evidence, origin, classification, note=None, actor=self.actor)
        derived = self._rederive(attempt)
        self._sync_lifecycle(attempt, derived)
        self._maybe_verify(attempt, row, classification, attribution, evidence)
        if evidence is EvidenceStrength.WEAK or not classification.confident:
            why = "classification needs confirmation" if not classification.confident else "weak evidence; recorded as provisional"
            if outcome is OutcomeKind.UNKNOWN:
                why = "submit outcome unknown; verify or confirm the attempt"
            self._set_status(row, SignalStatus.NEEDS_REVIEW, f"{why} ({classification.source.value}, {classification.confidence.value})")
            self.counts["needs_review"] += 1
        elif outcome is OutcomeKind.NEEDS_REVIEW:
            self._set_status(row, SignalStatus.NEEDS_REVIEW, "verification failed; a person decides")
            self.counts["needs_review"] += 1
        else:
            self._set_status(row, SignalStatus.APPLIED, f"{outcome.value} recorded with {evidence.value} evidence")
            self.counts["applied"] += 1

    def _record_event(self, attempt: ApplicationRow, row: Optional[SignalRow], outcome: OutcomeKind, evidence: EvidenceStrength, origin: EventOrigin, classification: Optional[Classification], note: Optional[str], actor: str, supersedes: Optional[OutcomeEventRow] = None) -> tuple[OutcomeEventRow, bool]:
        key = f"{row.id if row else 'manual'}:{attempt.id}:{outcome.value}:{origin.value}"
        existing = self.db.query(OutcomeEventRow).filter(OutcomeEventRow.tenant_id == self.tenant_id, OutcomeEventRow.dedupe_key == key).first()
        if existing is not None:
            return existing, False
        if row is not None and row.external_at is not None:
            event_at, basis = row.external_at, TimeBasis.EXTERNAL
        else:
            event_at, basis = (row.observed_at if row is not None else db_now()), TimeBasis.OBSERVED
        sequence = (self.db.query(func.max(OutcomeEventRow.sequence)).filter(OutcomeEventRow.tenant_id == self.tenant_id, OutcomeEventRow.application_id == attempt.id).scalar() or 0) + 1
        event = OutcomeEventRow(
            tenant_id=self.tenant_id,
            application_id=attempt.id,
            signal_id=row.id if row else None,
            attribution_id=row.attribution_id if row else None,
            outcome=outcome.value,
            evidence=evidence.value,
            origin=origin.value,
            category=classification.category.value if classification else None,
            event_at=event_at,
            time_basis=basis.value,
            observed_at=db_now(),
            sequence=sequence,
            dedupe_key=key,
            actor=actor,
            note=(note or "")[:512] or None,
            classifier_version=classification.classifier_version if classification else None,
            attribution_version=ATTRIBUTION_VERSION if row else None,
            rules_version=OUTCOME_RULES_VERSION,
        )
        try:
            with self.db.begin_nested():
                self.db.add(event)
                self.db.flush()
        except IntegrityError:
            existing = self.db.query(OutcomeEventRow).filter(OutcomeEventRow.tenant_id == self.tenant_id, OutcomeEventRow.dedupe_key == key).first()
            if existing is None:
                raise
            return existing, False
        if supersedes is not None and supersedes.id != event.id:
            supersedes.superseded_by_id = event.id
        self.repo.record("application_outcome", attempt.id, "event:" + outcome.value.lower(), actor, None, {"event_id": event.id, "signal_id": event.signal_id, "outcome": outcome.value, "evidence": evidence.value, "origin": origin.value, "event_at": event_at.isoformat() if event_at else None, "time_basis": basis.value}, note)
        return event, True

    def _events(self, application_id: str) -> list[OutcomeEventRow]:
        return self.db.query(OutcomeEventRow).filter(OutcomeEventRow.tenant_id == self.tenant_id, OutcomeEventRow.application_id == application_id).order_by(OutcomeEventRow.event_at.asc(), OutcomeEventRow.sequence.asc()).all()

    def _rederive(self, attempt: ApplicationRow) -> DerivedOutcome:
        events = self._events(attempt.id)
        derived = derive(attempt.id, events)
        row = self.db.query(ApplicationOutcomeRow).filter(ApplicationOutcomeRow.tenant_id == self.tenant_id, ApplicationOutcomeRow.application_id == attempt.id).first()
        before = None if row is None else {"current": row.current_outcome, "provisional": row.provisional_outcome, "needs_review": row.needs_review, "version": row.version}
        if row is None:
            row = ApplicationOutcomeRow(tenant_id=self.tenant_id, application_id=attempt.id, opportunity_id=attempt.opportunity_id, candidate_opportunity_id=attempt.candidate_opportunity_id, rules_version=OUTCOME_RULES_VERSION, version=0)
            self.db.add(row)
        changed = before is None or before["current"] != derived.current.value or before["provisional"] != (derived.provisional.value if derived.provisional else None) or before["needs_review"] != derived.needs_review or row.event_count != derived.event_count
        row.current_outcome = derived.current.value
        row.provisional_outcome = derived.provisional.value if derived.provisional else None
        row.needs_review = derived.needs_review
        row.conflicts = list(derived.conflicts)
        row.event_count = derived.event_count
        row.basis_event_id = derived.basis_event_id
        row.last_event_at = derived.last_event_at
        row.derived_at = db_now()
        row.rules_version = OUTCOME_RULES_VERSION
        if changed:
            row.version = (row.version or 0) + 1
            self.db.flush()
            self.repo.record("application_outcome", attempt.id, "derived", self.actor, before, {"current": row.current_outcome, "provisional": row.provisional_outcome, "needs_review": row.needs_review, "conflicts": row.conflicts, "version": row.version, "rules_version": OUTCOME_RULES_VERSION}, "; ".join(derived.conflicts) or None)
        self.db.flush()
        return derived

    def _sync_lifecycle(self, attempt: ApplicationRow, derived: DerivedOutcome) -> None:
        """Only two lifecycle moves follow from outcomes, both guarded and never
        forced: → INTERVIEWING and → REJECTED. Withdrawal and closure are kept
        as outcomes for a person to act on (they may release a cap slot)."""
        target: Optional[ApplicationStatus] = None
        state: Optional[OpportunityState] = None
        if derived.current in (OutcomeKind.INTERVIEW_REQUESTED, OutcomeKind.INTERVIEW_SCHEDULED) and attempt.status in (ApplicationStatus.SUBMITTED.value, ApplicationStatus.VERIFIED.value):
            target, state = ApplicationStatus.INTERVIEWING, OpportunityState.INTERVIEWING
        elif derived.current is OutcomeKind.REJECTED and attempt.status in (ApplicationStatus.SUBMITTED.value, ApplicationStatus.VERIFIED.value, ApplicationStatus.INTERVIEWING.value):
            target, state = ApplicationStatus.REJECTED, OpportunityState.REJECTED
        if target is None:
            return
        try:
            self.attempts.transition(attempt, target, self.actor, f"outcome {derived.current.value} (event {derived.basis_event_id})", {"outcome_event_id": derived.basis_event_id})
        except ConflictError as exc:
            logger.info("Lifecycle not moved for %s: %s", attempt.id, exc.message)
            return
        if attempt.candidate_opportunity_id and state is not None:
            co = self.db.get(CandidateOpportunityRow, attempt.candidate_opportunity_id)
            if co is not None and co.state != state.value:
                try:
                    self.repo.transition(co, state, self.actor, f"outcome {derived.current.value}")
                except ConflictError:
                    self.repo.transition(co, state, self.actor, f"outcome {derived.current.value}", force=True)
        self.repo.record("application_attempt", attempt.id, "outcome:" + target.value.lower(), self.actor, None, {"status": attempt.status, "outcome_event_id": derived.basis_event_id, "reason": derived.current.value}, derived.current.value)

    def _maybe_verify(self, attempt: ApplicationRow, row: SignalRow, classification: Classification, attribution: AttributionResult, evidence: EvidenceStrength) -> None:
        """Strong external confirmation of a SUBMITTED / UNCERTAIN attempt verifies it
        through the Phase 6 path; anything weaker only records an outcome."""
        if classification.category not in _CONFIRMATION_CATEGORIES or evidence is not EvidenceStrength.STRONG or attribution.confidence is not Confidence.HIGH:
            return
        if attempt.status not in (ApplicationStatus.SUBMITTED.value, ApplicationStatus.UNCERTAIN.value):
            return
        run = (
            self.db.query(ExecutionRunRow)
            .filter(ExecutionRunRow.tenant_id == self.tenant_id, ExecutionRunRow.application_id == attempt.id, ExecutionRunRow.status.in_([ExecutionStatus.SUBMITTED.value, ExecutionStatus.UNKNOWN.value]))
            .order_by(ExecutionRunRow.run_number.desc())
            .first()
        )
        if run is None:
            return
        from app.execution.service import ExecutionService

        reference = next(iter((row.payload or {}).get("references") or []), None) or None
        try:
            ExecutionService(self.db, self.tenant_id, actor=self.actor).verify(run.id, VerificationResult(status=VerificationStatus.VERIFIED, method=VerificationMethod.CONFIRMATION_EMAIL, detail=f"{classification.category.value} signal {row.id} ({attribution.rule})"[:512], confirmation_reference=reference))
            self._audit(row, "verified_attempt", {"run_id": run.id, "application_id": attempt.id, "method": VerificationMethod.CONFIRMATION_EMAIL.value})
        except CareerOSError as exc:
            logger.warning("Confirmation signal %s could not verify run %s: %s", row.id, run.id, exc.message)

    # ------------------------------------------------------------------ #
    # human review
    # ------------------------------------------------------------------ #

    def get_signal(self, signal_id: str) -> Optional[SignalRow]:
        return self.db.query(SignalRow).filter(SignalRow.tenant_id == self.tenant_id, SignalRow.id == signal_id).first()

    def require_signal(self, signal_id: str) -> SignalRow:
        row = self.get_signal(signal_id)
        if row is None:
            raise NotFoundError(f"Signal not found: {signal_id}")
        return row

    def _require_attempt(self, application_id: str) -> ApplicationRow:
        attempt = self.db.query(ApplicationRow).filter(ApplicationRow.tenant_id == self.tenant_id, ApplicationRow.id == application_id).first()
        if attempt is None:
            raise NotFoundError(f"Application attempt not found: {application_id}")
        return attempt

    def link(self, signal_id: str, application_id: str, actor: str, note: Optional[str] = None) -> SignalRow:
        """A person attributes the signal; earlier attribution and events stay, superseded / retracted."""
        row = self.require_signal(signal_id)
        if row.status in (SignalStatus.MERGED.value,):
            raise ConflictError("A merged signal cannot be linked; link the signal it was merged into")
        attempt = self._require_attempt(application_id)
        current = self.db.get(SignalAttributionRow, row.attribution_id) if row.attribution_id else None
        if row.application_id and row.application_id != attempt.id:
            self._retract_events(row, f"relinked to {attempt.id} by {actor}")
        result = AttributionResult(status=AttributionStatus.MANUAL, application_id=attempt.id, opportunity_id=attempt.opportunity_id, candidate_opportunity_id=attempt.candidate_opportunity_id, rule="human_link", confidence=Confidence.HIGH, evidence={"actor": actor, "note": (note or "")[:200]}, explanation=f"linked by {actor}")
        self._store_attribution(row, result, actor, current)
        classification = self._classify(row, self._renormalize(row))
        if row.status == SignalStatus.IGNORED.value:
            row.status = SignalStatus.NEW.value
        self._apply(row, classification, result)
        self._audit(row, "review:link", {"application_id": attempt.id, "actor": actor, "note": note, "status": row.status})
        self.db.flush()
        return row

    def confirm_classification(self, signal_id: str, category: SignalCategory, actor: str, note: Optional[str] = None) -> SignalRow:
        row = self.require_signal(signal_id)
        if row.status == SignalStatus.MERGED.value:
            raise ConflictError("A merged signal cannot be classified")
        changed = row.category != category.value or row.classification_source != ClassificationSource.HUMAN.value
        if changed and row.application_id:
            self._retract_events(row, f"classification changed to {category.value} by {actor}")
        row.category = category.value
        row.confidence = Confidence.HIGH.value
        row.classification_source = ClassificationSource.HUMAN.value
        row.classifier_version = f"human:{CLASSIFIER_VERSION}"
        row.classification = {**(row.classification or {}), "confirmed_by": actor, "note": (note or "")[:200]}
        classification = Classification(category=category, confidence=Confidence.HIGH, source=ClassificationSource.HUMAN, classifier_version=row.classifier_version)
        if row.attribution_status in (AttributionStatus.MATCHED.value, AttributionStatus.MANUAL.value) and row.application_id:
            current = self.db.get(SignalAttributionRow, row.attribution_id)
            result = AttributionResult(status=AttributionStatus(current.status), application_id=current.application_id, opportunity_id=current.opportunity_id, candidate_opportunity_id=current.candidate_opportunity_id, rule=current.rule, confidence=Confidence(current.confidence), explanation=current.explanation or "")
            if row.status == SignalStatus.IGNORED.value:
                row.status = SignalStatus.NEW.value
            self._apply(row, classification, result)
        else:
            normalized = self._renormalize(row)
            if row.status == SignalStatus.IGNORED.value:
                row.status = SignalStatus.NEW.value
            self._apply(row, classification, self._attribute(row, normalized))
        self._audit(row, "review:confirm_classification", {"category": category.value, "actor": actor, "note": note, "status": row.status})
        self.db.flush()
        return row

    def reject_classification(self, signal_id: str, actor: str, note: Optional[str] = None) -> SignalRow:
        row = self.require_signal(signal_id)
        self._retract_events(row, f"classification rejected by {actor}")
        row.category = SignalCategory.UNKNOWN.value
        row.confidence = Confidence.NONE.value
        row.classification_source = ClassificationSource.HUMAN.value
        row.classifier_version = f"human:{CLASSIFIER_VERSION}"
        row.classification = {**(row.classification or {}), "rejected_by": actor, "note": (note or "")[:200]}
        self._set_status(row, SignalStatus.NEEDS_REVIEW, f"classification rejected by {actor}; confirm a category or ignore")
        self._audit(row, "review:reject_classification", {"actor": actor, "note": note})
        self.db.flush()
        return row

    def confirm_outcome(self, signal_id: str, outcome: OutcomeKind, actor: str, note: Optional[str] = None) -> SignalRow:
        row = self.require_signal(signal_id)
        if not row.application_id:
            raise ConflictError("Link the signal to an application before confirming an outcome")
        attempt = self._require_attempt(row.application_id)
        auto = self.db.query(OutcomeEventRow).filter(OutcomeEventRow.tenant_id == self.tenant_id, OutcomeEventRow.signal_id == row.id, OutcomeEventRow.application_id == attempt.id, OutcomeEventRow.origin != EventOrigin.HUMAN.value, OutcomeEventRow.retracted.is_(False), OutcomeEventRow.superseded_by_id.is_(None)).first()
        classification = Classification(category=SignalCategory(row.category), confidence=Confidence(row.confidence), source=ClassificationSource(row.classification_source or "rules"), classifier_version=row.classifier_version or CLASSIFIER_VERSION)
        self._record_event(attempt, row, outcome, EvidenceStrength.STRONG, EventOrigin.HUMAN, classification, note=note or f"confirmed by {actor}", actor=actor, supersedes=auto)
        derived = self._rederive(attempt)
        self._sync_lifecycle(attempt, derived)
        self._set_status(row, SignalStatus.APPLIED, f"{outcome.value} confirmed by {actor}")
        self._audit(row, "review:confirm_outcome", {"outcome": outcome.value, "actor": actor, "note": note, "application_id": attempt.id})
        self.db.flush()
        return row

    def ignore(self, signal_id: str, actor: str, note: Optional[str] = None) -> SignalRow:
        row = self.require_signal(signal_id)
        self._retract_events(row, f"ignored by {actor}")
        self._set_status(row, SignalStatus.IGNORED, (note or f"marked irrelevant by {actor}")[:256])
        self._audit(row, "review:ignore", {"actor": actor, "note": note})
        self.db.flush()
        return row

    def merge(self, signal_id: str, into_signal_id: str, actor: str, note: Optional[str] = None) -> SignalRow:
        row = self.require_signal(signal_id)
        target = self.require_signal(into_signal_id)
        if row.id == target.id:
            raise ValidationFailed("A signal cannot be merged into itself")
        if target.status == SignalStatus.MERGED.value:
            raise ConflictError("The target signal was itself merged; merge into its target")
        self._retract_events(row, f"merged into {target.id} by {actor}")
        row.merged_into_id = target.id
        self._set_status(row, SignalStatus.MERGED, f"duplicate of {target.id} (merged by {actor})")
        target.observation_count = (target.observation_count or 1) + 1
        target.last_observed_at = max(target.last_observed_at or target.observed_at, row.last_observed_at or row.observed_at)
        self.db.add(SignalObservationRow(tenant_id=self.tenant_id, signal_id=target.id, source=row.source, source_reference=row.source_reference, content_hash=row.content_hash, observed_at=row.observed_at, provenance={"merged_from": row.id, "actor": actor}))
        self._audit(row, "review:merge", {"into": target.id, "actor": actor, "note": note})
        self._audit(target, "observed", {"merged_from": row.id, "observation_count": target.observation_count})
        self.db.flush()
        return row

    def reprocess(self, signal_id: str, actor: str) -> SignalRow:
        row = self.require_signal(signal_id)
        if row.status not in _REPROCESSABLE:
            raise ConflictError(f"A {row.status} signal is not reprocessed; link or confirm it instead")
        self.attributor.index.refresh()
        self.process(row)
        self._audit(row, "review:reprocess", {"actor": actor, "status": row.status})
        self.db.flush()
        return row

    def _retract_events(self, row: SignalRow, reason: str) -> None:
        touched: dict[str, ApplicationRow] = {}
        for event in self.db.query(OutcomeEventRow).filter(OutcomeEventRow.tenant_id == self.tenant_id, OutcomeEventRow.signal_id == row.id, OutcomeEventRow.retracted.is_(False)).all():
            event.retracted = True
            event.retracted_reason = reason[:256]
            attempt = self.db.get(ApplicationRow, event.application_id)
            if attempt is not None:
                touched[attempt.id] = attempt
        self.db.flush()
        for attempt in touched.values():
            self._rederive(attempt)

    def _set_status(self, row: SignalRow, status: SignalStatus, reason: Optional[str]) -> None:
        row.status = status.value
        row.status_reason = (reason or "")[:256] or None

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #

    def _signals(self):
        return self.db.query(SignalRow).filter(SignalRow.tenant_id == self.tenant_id)

    def list_signals(self, status: Optional[SignalStatus] = None, source: Optional[SignalSource] = None, category: Optional[SignalCategory] = None, application_id: Optional[str] = None, outcome: Optional[OutcomeKind] = None, company: Optional[str] = None, days: Optional[int] = None, review: bool = False, limit: int = 100, offset: int = 0) -> tuple[list[SignalRow], int]:
        query = self._signals()
        if review:
            query = query.filter(SignalRow.status.in_(_REVIEW_STATUSES))
        if status is not None:
            query = query.filter(SignalRow.status == status.value)
        if source is not None:
            query = query.filter(SignalRow.source == source.value)
        if category is not None:
            query = query.filter(SignalRow.category == category.value)
        if application_id:
            query = query.filter(SignalRow.application_id == application_id)
        if outcome is not None:
            query = query.join(ApplicationOutcomeRow, (ApplicationOutcomeRow.application_id == SignalRow.application_id) & (ApplicationOutcomeRow.tenant_id == self.tenant_id)).filter(ApplicationOutcomeRow.current_outcome == outcome.value)
        if company:
            query = query.join(OpportunityRow, OpportunityRow.id == SignalRow.opportunity_id).filter(OpportunityRow.company.ilike(f"%{company.strip()}%"))
        if days:
            query = query.filter(SignalRow.observed_at >= db_now() - timedelta(days=days))
        total = query.count()
        rows = query.order_by(SignalRow.observed_at.desc(), SignalRow.id.desc()).offset(offset).limit(limit).all()
        return rows, total

    def review_queue(self, limit: int = 100) -> list[SignalRow]:
        return self.list_signals(review=True, limit=limit)[0]

    def attributions_for(self, signal_id: str) -> list[SignalAttributionRow]:
        return self.db.query(SignalAttributionRow).filter(SignalAttributionRow.tenant_id == self.tenant_id, SignalAttributionRow.signal_id == signal_id).order_by(SignalAttributionRow.created_at.asc(), SignalAttributionRow.id.asc()).all()

    def observations_for(self, signal_id: str) -> list[SignalObservationRow]:
        return self.db.query(SignalObservationRow).filter(SignalObservationRow.tenant_id == self.tenant_id, SignalObservationRow.signal_id == signal_id).order_by(SignalObservationRow.observed_at.asc(), SignalObservationRow.id.asc()).all()

    def events_for_signal(self, signal_id: str) -> list[OutcomeEventRow]:
        return self.db.query(OutcomeEventRow).filter(OutcomeEventRow.tenant_id == self.tenant_id, OutcomeEventRow.signal_id == signal_id).order_by(OutcomeEventRow.sequence.asc()).all()

    def outcome_history(self, application_id: str) -> tuple[Optional[ApplicationOutcomeRow], list[OutcomeEventRow]]:
        self._require_attempt(application_id)
        current = self.db.query(ApplicationOutcomeRow).filter(ApplicationOutcomeRow.tenant_id == self.tenant_id, ApplicationOutcomeRow.application_id == application_id).first()
        return current, self._events(application_id)

    def list_outcomes(self, current: Optional[OutcomeKind] = None, needs_review: Optional[bool] = None, limit: int = 100, offset: int = 0) -> tuple[list[ApplicationOutcomeRow], int]:
        query = self.db.query(ApplicationOutcomeRow).filter(ApplicationOutcomeRow.tenant_id == self.tenant_id)
        if current is not None:
            query = query.filter(ApplicationOutcomeRow.current_outcome == current.value)
        if needs_review is not None:
            query = query.filter(ApplicationOutcomeRow.needs_review.is_(needs_review))
        total = query.count()
        return query.order_by(ApplicationOutcomeRow.updated_at.desc(), ApplicationOutcomeRow.id.asc()).offset(offset).limit(limit).all(), total

    def trace(self, signal_id: str) -> SignalTrace:
        row = self.require_signal(signal_id)
        trace = SignalTrace(
            signal=Signal.model_validate(row),
            observations=[SignalObservation.model_validate(o) for o in self.observations_for(row.id)],
            attributions=[SignalAttribution.model_validate(a) for a in self.attributions_for(row.id)],
            outcome_events=[OutcomeEvent.model_validate(e) for e in self.events_for_signal(row.id)],
        )
        if row.application_id:
            attempt = self.db.query(ApplicationRow).filter(ApplicationRow.tenant_id == self.tenant_id, ApplicationRow.id == row.application_id).first()
            if attempt is not None:
                from app.execution.models import ApplicationAttempt, ExecutionRun

                trace.attempt = ApplicationAttempt.model_validate(attempt).model_dump(mode="json")
                current = self.db.query(ApplicationOutcomeRow).filter(ApplicationOutcomeRow.tenant_id == self.tenant_id, ApplicationOutcomeRow.application_id == attempt.id).first()
                trace.application_outcome = ApplicationOutcome.model_validate(current) if current else None
                opp = self.db.get(OpportunityRow, attempt.opportunity_id) if attempt.opportunity_id else None
                if opp is not None:
                    trace.opportunity = {"id": opp.id, "company": opp.company, "title": opp.title, "status": opp.status, "location_bucket": opp.location_bucket}
                co = self.db.get(CandidateOpportunityRow, attempt.candidate_opportunity_id) if attempt.candidate_opportunity_id else None
                if co is not None and co.tenant_id == self.tenant_id:
                    trace.candidate_opportunity = {"id": co.id, "state": co.state, "fit_band": co.fit_band, "fit_score": co.fit_score, "policy_admitted": co.policy_admitted, "fit_policy_version": co.fit_policy_version, "admission_policy_version": co.admission_policy_version, "gate_ruleset_version": co.gate_ruleset_version}
                if attempt.preparation_id:
                    from app.preparation.database.models import ApplicationPreparationRow

                    prep = self.db.get(ApplicationPreparationRow, attempt.preparation_id)
                    if prep is not None and prep.tenant_id == self.tenant_id:
                        trace.preparation = {"id": prep.id, "version": prep.version, "status": prep.status, "tailoring_level": prep.tailoring_level, "input_fingerprint": prep.input_fingerprint}
                runs = self.db.query(ExecutionRunRow).filter(ExecutionRunRow.tenant_id == self.tenant_id, ExecutionRunRow.application_id == attempt.id).order_by(ExecutionRunRow.run_number.asc()).all()
                trace.execution_runs = [ExecutionRun.model_validate(r).model_dump(mode="json", include={"id", "run_number", "executor_kind", "executor_version", "status", "outcome", "verification_status", "verification_method", "confirmation_reference", "started_at", "finished_at"}) for r in runs]
                events = self.db.query(ApplicationEventRow).filter(ApplicationEventRow.application_id == attempt.id).order_by(ApplicationEventRow.created_at.asc()).all()
                trace.application_events = [{"event_type": e.event_type, "from_status": e.from_status, "to_status": e.to_status, "created_at": e.created_at.isoformat() if e.created_at else None} for e in events]
        return trace

    def summary(self) -> SignalSummary:
        by_status = dict(self.db.query(SignalRow.status, func.count(SignalRow.id)).filter(SignalRow.tenant_id == self.tenant_id).group_by(SignalRow.status).all())
        by_source = dict(self.db.query(SignalRow.source, func.count(SignalRow.id)).filter(SignalRow.tenant_id == self.tenant_id).group_by(SignalRow.source).all())
        by_category = dict(self.db.query(SignalRow.category, func.count(SignalRow.id)).filter(SignalRow.tenant_id == self.tenant_id).group_by(SignalRow.category).all())
        outcomes = dict(self.db.query(ApplicationOutcomeRow.current_outcome, func.count(ApplicationOutcomeRow.id)).filter(ApplicationOutcomeRow.tenant_id == self.tenant_id).group_by(ApplicationOutcomeRow.current_outcome).all())
        return SignalSummary(tenant_id=self.tenant_id, by_status=by_status, by_source=by_source, by_category=by_category, outcomes_by_current=outcomes, review_queue=by_status.get(SignalStatus.NEEDS_REVIEW.value, 0) + by_status.get(SignalStatus.UNMATCHED.value, 0), unmatched=by_status.get(SignalStatus.UNMATCHED.value, 0))

    def purge_excerpts(self, older_than_days: int) -> int:
        """Drop stored excerpts of settled signals older than N days (hashes,
        classification, attribution and outcomes stay)."""
        cutoff = db_now() - timedelta(days=max(0, older_than_days))
        rows = self._signals().filter(SignalRow.status.in_([SignalStatus.APPLIED.value, SignalStatus.IGNORED.value, SignalStatus.MERGED.value]), SignalRow.observed_at < cutoff, SignalRow.excerpt.isnot(None)).all()
        for row in rows:
            row.excerpt = None
            row.payload = {**(row.payload or {}), "excerpt_purged": True}
        self.db.flush()
        return len(rows)

    # ------------------------------------------------------------------ #

    def _audit(self, row: SignalRow, action: str, after: dict[str, Any]) -> None:
        self.repo.record("signal", row.id, action, self.actor, None, {"status": row.status, **after}, after.get("explanation") or after.get("note") or row.status_reason)


class SignalIngestionService(SignalInboxService):
    """The connector boundary: ``ingest(SignalIngest)`` / ``ingest_email(EmailMessage)``.
    A future mailbox or extension connector feeds this and nothing else."""


__all__ = ["SignalIngestionService", "SignalInboxService"]
