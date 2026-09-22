"""The Phase 1/4/8b truth boundary holds for external evidence: a signal can
say an event happened; it can never create a candidate fact, and neither can
the AI classifier."""

from app.career.database.models import AnswerBankEntryRow, EvidenceNodeRow
from app.pipeline.repository import OpportunityRepository
from app.signals.ai import GatewaySignalClassifier
from app.signals.models import AttributionHints
from app.signals.service import SignalInboxService
from tests.ai.conftest import TENANT_ON, make_gateway
from tests.signals.conftest import email, submitted


def _graph(db, tenant_id):
    nodes = db.query(EvidenceNodeRow).filter(EvidenceNodeRow.tenant_id == tenant_id).order_by(EvidenceNodeRow.key).all()
    answers = db.query(AnswerBankEntryRow).filter(AnswerBankEntryRow.tenant_id == tenant_id).order_by(AnswerBankEntryRow.question_key).all()
    return [(n.key, n.claim, n.verification_status, n.version) for n in nodes], [(a.question_key, a.answer, a.version) for a in answers]


def test_external_signal_records_an_event_but_never_a_candidate_fact(inbox, db_session, tenant_id, evidence, opportunities):
    before = _graph(db_session, tenant_id)
    attempt = submitted(db_session, tenant_id, opportunities, company="Truth Co")
    row, _ = inbox.ingest_email(email("Thank you for applying", "Thank you for applying for Senior Software Engineer. We note your 5 years of Kubernetes experience and your AWS certification. Application ID: TR-100", hints=AttributionHints(application_id=attempt.id)))
    assert row.status == "APPLIED"
    assert inbox.outcome_history(attempt.id)[0].current_outcome == "APPLICATION_RECEIVED"
    after = _graph(db_session, tenant_id)
    assert after == before, "no evidence node or answer changed"
    assert not any("kubernetes" in claim.lower() or "aws" in claim.lower() for _, claim, _, _ in after[0])
    audit = OpportunityRepository(db_session, tenant_id).list_audit("evidence_node")
    assert audit == [] or all(e.created_at <= row.created_at for e in audit), "no evidence audit event was written by the inbox"


def test_ai_classification_cannot_create_evidence_or_relationships(db_session, tenant_id, evidence, opportunities):
    before = _graph(db_session, tenant_id)
    attempt = submitted(db_session, tenant_id, opportunities, company="Truth AI")
    gateway, _ = make_gateway([f'{{"category": "INTERVIEW_INVITATION", "confidence": "HIGH", "reason": "application {attempt.id}", "application_id": "{attempt.id}", "candidate_experience_years": 9}}'], candidate_providers=("scripted",))
    service = SignalInboxService(db_session, tenant_id, actor="test", classifier=GatewaySignalClassifier(gateway, tenant_id, TENANT_ON))
    row, _ = service.ingest_email(email("Hello", "Just checking in about the sky.", sender="someone@unknown.example"))
    assert row.classification_source == "ai" and row.category == "INTERVIEW_INVITATION" and row.confidence == "MEDIUM"
    assert row.attribution_status == "UNMATCHED" and row.application_id is None, "the model's application id is never used for attribution"
    assert "candidate_experience_years" not in str(row.classification) and "candidate_experience_years" not in str(row.ai)
    assert _graph(db_session, tenant_id) == before
    assert service.outcome_history(attempt.id)[1] == []


def test_ai_suggestion_on_a_matched_signal_is_weak_and_provisional(db_session, tenant_id, evidence, opportunities):
    attempt = submitted(db_session, tenant_id, opportunities, company="Truth Weak")
    gateway, _ = make_gateway(['{"category": "REJECTION", "confidence": "HIGH"}'], candidate_providers=("scripted",))
    service = SignalInboxService(db_session, tenant_id, actor="test", classifier=GatewaySignalClassifier(gateway, tenant_id, TENANT_ON))
    service.ingest_email(email("Interview", "We'd like to invite you to an interview.", message_id="<s@x>", hints=AttributionHints(application_id=attempt.id)))
    row, _ = service.ingest_email(email("Hmm", "Just checking in about the weather.", message_id="<w@x>", hints=AttributionHints(application_id=attempt.id)))
    assert row.classification_source == "ai" and row.status == "NEEDS_REVIEW"
    current, events = service.outcome_history(attempt.id)
    assert [e.evidence for e in events] == ["STRONG", "WEAK"] and current.current_outcome == "INTERVIEW_REQUESTED" and current.provisional_outcome == "REJECTED" and current.needs_review
    db_session.refresh(attempt)
    assert attempt.status == "INTERVIEWING", "a weak rejection never moves the lifecycle"


def test_unsupported_claims_in_signals_never_reach_preparation_inputs(db_session, tenant_id, evidence, answered_bank, opportunities):
    """Preparation reads the Evidence Graph and answer bank only; a signal cannot alter its fingerprint."""
    from app.preparation.evidence import EvidenceSnapshot

    snapshot_before = EvidenceSnapshot.load(answered_bank).fingerprint
    inbox = SignalInboxService(db_session, tenant_id, actor="test")
    attempt = submitted(db_session, tenant_id, opportunities, company="Truth Prep")
    inbox.ingest_email(email("Offer", "Congratulations on your 10 years of leadership. We'd like to invite you to an interview.", hints=AttributionHints(application_id=attempt.id)))
    assert EvidenceSnapshot.load(answered_bank).fingerprint == snapshot_before
    assert all("leadership" not in (a.answer or "").lower() for a in db_session.query(AnswerBankEntryRow).filter(AnswerBankEntryRow.tenant_id == tenant_id).all())
