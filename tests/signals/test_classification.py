"""Deterministic classification (pure) and the optional AI path through the gateway."""

import pytest

from app.ai.gateway import reset_gateway
from app.ai.models import AIOperation, AIStatus
from app.signals.ai import GatewaySignalClassifier, classify_prompt
from app.signals.classify import RULES, classify_text
from app.signals.models import CLASSIFIER_VERSION, Confidence, SignalCategory, SignalStatus
from tests.ai.conftest import TENANT_ON, make_gateway
from tests.signals.conftest import email

CASES = [
    ("Thank you for applying to Acme", "Thanks for applying for the Backend Engineer role. We have received your application and will review it shortly.", SignalCategory.APPLICATION_RECEIVED, Confidence.HIGH),
    ("Application confirmation", "Application ID: ACME-12345. Your application has been submitted successfully.", SignalCategory.APPLICATION_CONFIRMATION, Confidence.HIGH),
    ("Update on your application", "Thank you for your interest in Acme. Unfortunately, we have decided to move forward with other candidates whose experience more closely matches our needs.", SignalCategory.REJECTION, Confidence.HIGH),
    ("Thank you for interviewing", "Thank you for interviewing with us. Unfortunately we will not be moving forward with your application at this time.", SignalCategory.REJECTION, Confidence.HIGH),
    ("Interview invitation: Backend Engineer", "We'd love to invite you to a phone screen with our engineering team. Please book a time using the link below.", SignalCategory.INTERVIEW_INVITATION, Confidence.HIGH),
    ("Next step: online assessment", "Please complete the coding challenge on HackerRank within 5 days.", SignalCategory.ASSESSMENT, Confidence.HIGH),
    ("Reaching out about a role", "Hi! I'm a technical recruiter at Acme and came across your profile. Would love to connect about an opening.", SignalCategory.RECRUITER_CONTACT, Confidence.MEDIUM),
    ("Additional information needed", "Could you please provide your notice period and upload a copy of your degree certificate?", SignalCategory.INFORMATION_REQUEST, Confidence.HIGH),
    ("Application withdrawn", "This confirms that your application for Backend Engineer has been withdrawn as requested.", SignalCategory.WITHDRAWAL, Confidence.HIGH),
    ("Application status", "The status of your application has changed: your application is currently under review.", SignalCategory.STATUS_UPDATE, Confidence.HIGH),
    ("Position closed", "This position is no longer accepting applications.", SignalCategory.DUPLICATE_OR_CLOSED, Confidence.HIGH),
    ("Jobs you may be interested in", "Here are new jobs matching your preferences. Unsubscribe from these emails at any time.", SignalCategory.OTHER, Confidence.HIGH),
    ("Hello", "Just checking in about the weather.", SignalCategory.UNKNOWN, Confidence.NONE),
]


@pytest.mark.parametrize("subject,body,category,confidence", CASES, ids=[c[2].value for c in CASES])
def test_rules_classify_the_common_categories(subject, body, category, confidence):
    result = classify_text(subject, body)
    assert result.category is category and result.confidence is confidence and result.classifier_version == CLASSIFIER_VERSION
    assert result.source.value == "rules"
    if category not in (SignalCategory.UNKNOWN,):
        assert result.matched_rules, "every non-UNKNOWN classification names the rules that fired"


def test_classification_is_deterministic_and_rule_ids_are_unique():
    ids = [r.id for r in RULES]
    assert len(ids) == len(set(ids))
    a = classify_text("Interview", "We'd like to invite you to an interview. Thank you for applying!")
    b = classify_text("Interview", "We'd like to invite you to an interview. Thank you for applying!")
    assert a == b and a.category is SignalCategory.INTERVIEW_INVITATION and "APPLICATION_RECEIVED" in a.competing


def test_incompatible_categories_reduce_confidence():
    mixed = classify_text("News", "You have withdrawn your application. Also, we would like to invite you to an interview.")
    assert mixed.category is SignalCategory.WITHDRAWAL and mixed.confidence is Confidence.MEDIUM and "INTERVIEW_INVITATION" in mixed.competing


def test_marketing_sender_becomes_other_unless_a_real_category_matches():
    assert classify_text("Weekly picks", "Some roles for you", marketing_sender=True).category is SignalCategory.OTHER
    assert classify_text("Interview", "We'd like to invite you to an interview", marketing_sender=True).category is SignalCategory.INTERVIEW_INVITATION


# ------------------------------------------------------------- AI path


def test_ai_disabled_path_keeps_deterministic_result_and_makes_no_calls(inbox):
    row, _ = inbox.ingest_email(email("Hello", "Just checking in about the weather."))
    assert row.category == "UNKNOWN" and row.status == SignalStatus.NEEDS_REVIEW.value and row.ai == {}
    assert inbox.ai_metadata() == {"used": False, "calls": 0}


def test_ai_is_not_consulted_when_rules_are_confident(db_session, tenant_id):
    from app.signals.service import SignalInboxService

    gateway, provider = make_gateway(['{"category": "REJECTION", "confidence": "HIGH"}'], candidate_providers=("scripted",))
    classifier = GatewaySignalClassifier(gateway, tenant_id, TENANT_ON)
    service = SignalInboxService(db_session, tenant_id, actor="test", classifier=classifier)
    row, _ = service.ingest_email(email("Interview", "We'd love to invite you to an interview next week."))
    assert row.category == "INTERVIEW_INVITATION" and row.classification_source == "rules" and classifier.calls == 0 and gateway.stats()["provider_calls"] == 0


def test_ai_classifies_the_ambiguous_case_but_is_never_authoritative(db_session, tenant_id):
    from app.signals.service import SignalInboxService

    gateway, provider = make_gateway(['{"category": "REJECTION", "confidence": "HIGH", "reason": "says no"}'], candidate_providers=("scripted",))
    classifier = GatewaySignalClassifier(gateway, tenant_id, TENANT_ON)
    service = SignalInboxService(db_session, tenant_id, actor="test", classifier=classifier)
    row, _ = service.ingest_email(email("Hello", "Just checking in about the weather."))
    assert row.category == "REJECTION" and row.classification_source == "ai" and row.confidence == "MEDIUM", "AI can suggest, never HIGH"
    assert row.status == SignalStatus.UNMATCHED.value, "no application matched; the AI never invents a relationship"
    assert row.ai["status"] == "OK" and row.ai["operation"] == AIOperation.CLASSIFY_SIGNAL.value and row.ai["gateway_version"] and row.ai["scope"] == "candidate"
    assert "latency_ms" in row.ai and "cache_hit" in row.ai and "prompt" not in row.ai and "weather" not in str(row.ai)
    assert row.classification["ai"]["rules"]["category"] == "UNKNOWN"
    assert classifier.metadata()["calls"] == 1 and gateway.stats()["provider_calls"] == 1
    # the prompt carries only a bounded excerpt, never credentials or the whole thread
    assert len(classify_prompt("s", "x" * 10_000, None)) < 2_500


def test_ai_malformed_or_unknown_category_falls_back(db_session, tenant_id):
    from app.signals.service import SignalInboxService

    gateway, _ = make_gateway(["not json", '{"category": "OFFER_LETTER", "confidence": "HIGH"}', '{"category": "REJECTION", "confidence": "HIGH"}'], candidate_providers=("scripted",))
    classifier = GatewaySignalClassifier(gateway, tenant_id, TENANT_ON)
    service = SignalInboxService(db_session, tenant_id, actor="test", classifier=classifier)
    a, _ = service.ingest_email(email("Hello", "Just checking in about the weather.", message_id="<a@x>"))
    assert a.category == "UNKNOWN" and a.ai["status"] == AIStatus.MALFORMED.value and a.status == SignalStatus.NEEDS_REVIEW.value
    b, _ = service.ingest_email(email("Hello", "Just checking in about the sky.", message_id="<b@x>"))
    assert b.category == "UNKNOWN" and b.ai["status"] == AIStatus.MALFORMED.value and "not in the enum" in b.ai["fallback_reason"]
    stats = gateway.stats()
    assert stats["requests"] == 2 and stats["by_status"] == {"MALFORMED": 1, "OK": 1}, "the gateway saw two calls; the enum check rejected the second"


def test_ai_unavailable_or_timed_out_means_needs_review(db_session, tenant_id):
    from app.ai.providers import ProviderError
    from app.signals.service import SignalInboxService

    gateway, _ = make_gateway([ProviderError("slow", timeout=True)], candidate_providers=("scripted",))
    classifier = GatewaySignalClassifier(gateway, tenant_id, TENANT_ON)
    service = SignalInboxService(db_session, tenant_id, actor="test", classifier=classifier)
    row, _ = service.ingest_email(email("Hello", "Just checking in about the weather."))
    assert row.category == "UNKNOWN" and row.ai["status"] == AIStatus.TIMEOUT.value and row.status == SignalStatus.NEEDS_REVIEW.value


def test_ai_budget_and_hosted_provider_refusal(db_session, tenant_id):
    from app.signals.service import SignalInboxService

    gateway, _ = make_gateway(['{"category": "REJECTION", "confidence": "HIGH"}'] * 5, max_calls=1, candidate_providers=("scripted",))
    classifier = GatewaySignalClassifier(gateway, tenant_id, TENANT_ON)
    service = SignalInboxService(db_session, tenant_id, actor="test", classifier=classifier)
    a, _ = service.ingest_email(email("Hello", "Just checking in about the weather.", message_id="<a@x>"))
    b, _ = service.ingest_email(email("Hello", "Just checking in about the sky.", message_id="<b@x>"))
    assert a.ai["status"] == "OK" and b.ai["status"] == AIStatus.BUDGET_EXHAUSTED.value and gateway.stats()["provider_calls"] == 1
    # candidate-side text never reaches a provider that is not allowed for candidate data
    from app.ai.providers import ScriptedProvider

    remote = ScriptedProvider(['{"category": "REJECTION"}'])
    remote.local = False  # a hosted provider not allowed for candidate data
    hosted, _ = make_gateway(scripted=remote, candidate_providers=("ollama",))
    refused = GatewaySignalClassifier(hosted, tenant_id, TENANT_ON)
    result, meta = refused.classify("Hello", "weather", "acme.com")
    assert result is None and meta["status"] == AIStatus.REFUSED.value and hosted.stats()["provider_calls"] == 0


def test_tenant_switch_selects_the_gateway_classifier(db_session, tenant_id):
    from app.ai.models import TenantAISettings
    from app.signals.ai import get_signal_classifier

    gateway, _ = make_gateway(['{"category": "REJECTION"}'], candidate_providers=("scripted",))
    reset_gateway(gateway)
    assert get_signal_classifier(tenant_id, TenantAISettings(enabled=False)) is None
    assert get_signal_classifier(tenant_id, TenantAISettings(enabled=True)) is not None
    reset_gateway(None)
