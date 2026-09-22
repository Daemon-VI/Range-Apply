"""Phase 10 benchmarks: 1,000 (5,000 opt-in) signals against 200 executed
attempts, duplicate-heavy delivery, deterministic classification and
attribution in isolation, outcome derivation, and the AI-disabled path.

Expected AI calls: zero, everywhere.
"""

import os
import time
import tracemalloc

import pytest

from app.ai.gateway import AIGateway, GlobalAIConfig, UsageSink, reset_gateway
from app.signals.classify import classify_text
from app.signals.database.models import (
    ApplicationOutcomeRow,
    OutcomeEventRow,
    SignalAttributionRow,
    SignalObservationRow,
    SignalRow,
)
from app.signals.models import AttributionHints, EmailMessage
from app.signals.outcomes import derive
from app.signals.service import SignalInboxService
from tests.signals.conftest import submitted

BODIES = [
    ("Thank you for applying", "Thank you for applying for the {title} role at {company}. We have received your application and will review it shortly. Application ID: {ref}"),
    ("Interview invitation", "We'd like to invite you to an interview for the {title} position at {company}. Please book a time."),
    ("Update on your application", "Thank you for your interest in {company}. Unfortunately we have decided to move forward with other candidates."),
    ("Next step: assessment", "Please complete the coding challenge on HackerRank for {company} within 5 days."),
    ("Application status", "The status of your application at {company} has changed: your application is currently under review."),
    ("Hello", "Just checking in from {company} about the weather and other things."),
    ("Jobs you may like", "New jobs matching your preferences. Unsubscribe from these emails at any time."),
    ("Additional information", "Could you please provide your notice period for the {title} role at {company}?"),
]


def _counts(db, tenant_id):
    return {
        "signals": db.query(SignalRow).filter(SignalRow.tenant_id == tenant_id).count(),
        "observations": db.query(SignalObservationRow).filter(SignalObservationRow.tenant_id == tenant_id).count(),
        "attributions": db.query(SignalAttributionRow).filter(SignalAttributionRow.tenant_id == tenant_id).count(),
        "outcome_events": db.query(OutcomeEventRow).filter(OutcomeEventRow.tenant_id == tenant_id).count(),
        "application_outcomes": db.query(ApplicationOutcomeRow).filter(ApplicationOutcomeRow.tenant_id == tenant_id).count(),
    }


def _run(db_session, tenant_id, opportunities, n_signals: int, n_attempts: int = 200, duplicates: int = 1, trace_memory: bool = False) -> dict:
    """``trace_memory`` runs under tracemalloc (roughly halves throughput); the
    duplicate-heavy run reports memory, the volume runs report clean throughput."""
    gateway = AIGateway(config=GlobalAIConfig(enabled=False, persist_usage=False), sink=UsageSink(persist=False))
    reset_gateway(gateway)
    attempts = [submitted(db_session, tenant_id, opportunities, company=f"Perf Co {i}", title="Backend Engineer" if i % 2 else "Data Engineer", reference=f"PERF-{i:05d}") for i in range(n_attempts)]
    db_session.commit()
    messages = []
    for i in range(n_signals):
        attempt = attempts[i % n_attempts]
        subject, body = BODIES[i % len(BODIES)]
        text = body.format(title=attempt.title_name, company=attempt.company_name, ref=f"PERF-{i % n_attempts:05d}")
        hints = AttributionHints(application_id=attempt.id) if i % 3 == 0 else AttributionHints()
        messages.append(EmailMessage(message_id=f"<perf-{i}@x>", sender=f"hr@perfco{i % n_attempts}.com", subject=subject, text=text, hints=hints))
    service = SignalInboxService(db_session, tenant_id, actor="perf")
    before = _counts(db_session, tenant_id)
    peak = 0
    if trace_memory:
        tracemalloc.start()
    started = time.perf_counter()
    for _ in range(duplicates):
        for message in messages:
            service.ingest_email(message)
        db_session.commit()
    seconds = time.perf_counter() - started
    if trace_memory:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    after = _counts(db_session, tenant_id)
    writes = {k: after[k] - before[k] for k in after}
    delivered = n_signals * duplicates
    results = {
        "signals_delivered": delivered,
        "unique_signals": writes["signals"],
        "seconds": round(seconds, 3),
        "per_second": round(delivered / max(seconds, 1e-9)),
        "db_rows_written": writes,
        "by_status": dict(service.counts),
        "ai_calls": gateway.stats()["provider_calls"],
        "ai_requests": gateway.stats()["requests"],
        "python_peak_memory_mb": round(peak / 1024 / 1024, 2) if trace_memory else None,
    }
    print(f"\nSIGNAL BENCHMARK ({n_signals} x{duplicates}):", results)
    reset_gateway(None)
    assert results["ai_calls"] == 0 and results["ai_requests"] == 0
    assert writes["signals"] == n_signals and writes["observations"] == delivered
    return results


@pytest.mark.perf
def test_signals_1000_ai_disabled(db_session, tenant_id, opportunities):
    results = _run(db_session, tenant_id, opportunities, 1000)
    assert results["seconds"] < 120 and results["by_status"]["applied"] > 0 and results["by_status"]["ingested"] == 1000


@pytest.mark.perf
@pytest.mark.skipif(os.environ.get("CAREEROS_PERF") != "1", reason="set CAREEROS_PERF=1 for the 5,000-signal run")
def test_signals_5000_ai_disabled(db_session, tenant_id, opportunities):
    _run(db_session, tenant_id, opportunities, 5000)


@pytest.mark.perf
def test_duplicate_heavy_ingestion(db_session, tenant_id, opportunities):
    results = _run(db_session, tenant_id, opportunities, 300, n_attempts=50, duplicates=4, trace_memory=True)
    assert results["unique_signals"] == 300 and results["db_rows_written"]["observations"] == 1200 and results["by_status"]["duplicates"] == 900
    assert results["db_rows_written"]["outcome_events"] <= 300, "no duplicate outcome events"


@pytest.mark.perf
def test_classification_attribution_and_derivation_in_isolation(db_session, tenant_id, opportunities):
    from app.signals.attribution import Attributor
    from app.signals.models import SignalIngest, SignalSource
    from app.signals.normalize import NormalizedSignal
    from tests.signals.test_outcomes import E

    attempts = [submitted(db_session, tenant_id, opportunities, company=f"Iso Co {i}", reference=f"ISO-{i:04d}") for i in range(100)]
    db_session.commit()
    n = 5000
    started = time.perf_counter()
    for i in range(n):
        subject, body = BODIES[i % len(BODIES)]
        classify_text(subject, body.format(title="Backend Engineer", company="Iso Co", ref="ISO-1"))
    classify_seconds = time.perf_counter() - started
    attributor = Attributor(db_session, tenant_id)
    normalized = [NormalizedSignal(SignalIngest(source=SignalSource.EMAIL, subject="Thanks", text=f"Thank you for applying to {attempts[i % 100].company_name}. Application ID: ISO-{i % 100:04d}")) for i in range(1000)]
    started = time.perf_counter()
    matched = sum(1 for s in normalized if attributor.attribute(s).application_id)
    attribute_seconds = time.perf_counter() - started
    events = [E(["SUBMITTED", "APPLICATION_RECEIVED", "UNDER_REVIEW", "INTERVIEW_REQUESTED", "REJECTED"][i % 5], ["STRONG", "MODERATE", "WEAK"][i % 3]) for i in range(20)]
    started = time.perf_counter()
    for _ in range(5000):
        derive("a", events)
    derive_seconds = time.perf_counter() - started
    results = {"classify": {"n": n, "seconds": round(classify_seconds, 3), "per_second": round(n / max(classify_seconds, 1e-9))}, "attribute": {"n": 1000, "seconds": round(attribute_seconds, 3), "per_second": round(1000 / max(attribute_seconds, 1e-9)), "matched": matched}, "derive": {"n": 5000, "events_each": 20, "seconds": round(derive_seconds, 3), "per_second": round(5000 / max(derive_seconds, 1e-9))}, "ai_calls": 0}
    print("\nSIGNAL COMPONENT BENCHMARK:", results)
    assert matched == 1000 and classify_seconds < 30 and attribute_seconds < 30 and derive_seconds < 30
