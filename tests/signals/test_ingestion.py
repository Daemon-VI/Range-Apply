"""Signal ingestion: unique / duplicate / replay / content hash / source identity /
tenant isolation / malformed input / privacy of what is stored."""

import pytest

from app.core.errors import ValidationFailed
from app.pipeline.repository import OpportunityRepository
from app.signals.database.models import SignalObservationRow, SignalRow
from app.signals.models import (
    AttributionHints,
    EmailMessage,
    SignalIngest,
    SignalSource,
    SignalStatus,
)
from app.signals.normalize import (
    content_hash,
    email_to_ingest,
    employer_domain,
    extract_references,
    normalize_text,
    redact_credentials,
    sender_domain,
    strip_html,
)
from tests.signals.conftest import email, manual


def test_unique_signal_then_duplicate_delivery_and_replay(inbox, db_session):
    msg = email("Thank you for applying", "We have received your application for Backend Engineer.", message_id="<m1@acme.com>")
    first, created = inbox.ingest_email(msg)
    assert created and first.source == "EMAIL" and first.source_reference == "<m1@acme.com>" and first.observation_count == 1
    again, created_again = inbox.ingest_email(msg)
    assert not created_again and again.id == first.id and again.observation_count == 2
    third, _ = inbox.ingest_email(msg)
    assert third.id == first.id and third.observation_count == 3
    assert db_session.query(SignalRow).filter(SignalRow.tenant_id == inbox.tenant_id).count() == 1
    observations = db_session.query(SignalObservationRow).filter(SignalObservationRow.signal_id == first.id).all()
    assert len(observations) == 3, "every delivery is kept as provenance"
    actions = [e.action for e in OpportunityRepository(db_session, inbox.tenant_id).list_audit("signal", first.id)]
    assert actions.count("observed") == 2 and "ingested" in actions


def test_same_content_without_message_id_dedupes_by_hash_but_different_text_does_not(inbox, db_session):
    a, created_a = inbox.ingest_email(email("Update", "Unfortunately we will not be moving forward with your application."))
    b, created_b = inbox.ingest_email(email("Update", "Unfortunately we will not be moving forward with your application."))
    c, created_c = inbox.ingest_email(email("Update", "Unfortunately we will not be moving forward with your application for the Data role."))
    assert created_a and not created_b and a.id == b.id and a.dedupe_key.startswith("EMAIL:hash:")
    assert created_c and c.id != a.id, "similar wording is not the same signal"


def test_stable_reference_wins_over_content_changes(inbox):
    a, _ = inbox.ingest_email(email("Status", "Your application is under review.", message_id="<same@acme.com>"))
    b, created = inbox.ingest_email(email("Status", "Your application is under review. (resent with a footer)", message_id="<same@acme.com>"))
    assert not created and a.id == b.id and a.observation_count == 2
    obs = inbox.observations_for(a.id)
    assert obs[-1].provenance.get("content_changed") is True and obs[-1].content_hash != a.content_hash


def test_source_identity_separates_sources(inbox):
    a, _ = inbox.ingest(SignalIngest(source=SignalSource.STATUS_PAGE, source_reference="obs-1", subject="Status", text="Under review"))
    b, created = inbox.ingest(SignalIngest(source=SignalSource.EXTENSION, source_reference="obs-1", subject="Status", text="Under review"))
    assert created and a.id != b.id and a.dedupe_key != b.dedupe_key


def test_tenant_isolation_of_signals(inbox, other_inbox, db_session):
    a, _ = inbox.ingest_email(email("Hi", "Thank you for applying.", message_id="<shared@acme.com>"))
    b, created = other_inbox.ingest_email(email("Hi", "Thank you for applying.", message_id="<shared@acme.com>"))
    assert created and a.id != b.id, "the same message for two tenants is two signals"
    assert other_inbox.get_signal(a.id) is None and inbox.get_signal(b.id) is None
    assert [s.id for s in inbox.list_signals()[0]] == [a.id]
    with pytest.raises(Exception):
        other_inbox.require_signal(a.id)


def test_malformed_input_is_refused(inbox):
    with pytest.raises(ValidationFailed):
        inbox.ingest(SignalIngest(source=SignalSource.EMAIL, subject="x", text="y", category="REJECTION"))
    with pytest.raises(ValueError):
        EmailMessage(sender="", subject="x")
    with pytest.raises(ValueError):
        SignalIngest(source="CARRIER_PIGEON", text="x")


def test_content_hash_is_deterministic_and_content_sensitive():
    assert content_hash("EMAIL", "acme.com", "Hi", "body") == content_hash("EMAIL", "ACME.com", " hi ", "body")
    assert content_hash("EMAIL", "acme.com", "Hi", "body") != content_hash("EMAIL", "acme.com", "Hi", "body2")
    assert content_hash("EMAIL", "acme.com", "Hi", "body") != content_hash("MANUAL", "acme.com", "Hi", "body")


def test_normalization_strips_html_quotes_and_signatures():
    html = "<html><style>x{}</style><body><p>Thank you for applying.</p><br><script>alert(1)</script><div>We will review your application.</div></body></html>"
    text = normalize_text(strip_html(html))
    assert "alert" not in text and "Thank you for applying." in text and "We will review your application." in text
    quoted = "Thanks, we received it.\n\nOn Mon, Jan 1 wrote:\n> old text\n> more"
    assert normalize_text(quoted) == "Thanks, we received it."
    signed = "We will be in touch.\nBest regards,\nSam\nRecruiter"
    assert normalize_text(signed) == "We will be in touch."


def test_credentials_are_redacted_and_never_stored(inbox):
    body = "Your application is under review. Password: hunter2 for the portal. Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"
    assert "hunter2" not in redact_credentials(body) and "abcdefghijklmnop" not in redact_credentials(body)
    row, _ = inbox.ingest_email(email("Portal", body, headers={"Authorization": "Bearer secret-token-value", "Cookie": "session=abc"}))
    assert "hunter2" not in (row.excerpt or "") and "secret-token" not in str(row.payload) and "session=abc" not in str(row.payload)
    assert "headers" not in row.payload and "html" not in row.payload


def test_excerpt_is_bounded_and_html_is_not_stored(inbox, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "signal_excerpt_chars", 120)
    long_body = "We have received your application. " * 50
    row, _ = inbox.ingest_email(email("Long", None, html="<p>" + long_body + "</p>"))
    assert row.excerpt is not None and len(row.excerpt) <= 120 and "<p>" not in row.excerpt


def test_email_to_ingest_reads_only_the_needed_headers():
    msg = EmailMessage(message_id="<m2@x>", sender="Acme Talent <talent@acme.com>", subject="Re: interview", text="See you Monday", headers={"In-Reply-To": "<m1@x>", "Date": "2026-09-10T10:00:00Z", "Authorization": "Bearer nope", "List-Unsubscribe": "<mailto:x>"})
    req = email_to_ingest(msg)
    assert req.hints.in_reply_to == "<m1@x>" and req.external_at is not None and req.external_at.year == 2026
    assert req.payload.get("list_unsubscribe") is True and "Authorization" not in str(req.payload) and "nope" not in str(req.model_dump())


def test_helpers():
    assert sender_domain("Acme <no-reply@jobs.acme.co.uk>") == "jobs.acme.co.uk" and employer_domain("jobs.acme.co.uk") == "acme.co.uk"
    assert employer_domain("notifications.greenhouse.io") is None and employer_domain("mail.lever.co") is None
    assert extract_references("Your application ID: ACME-12345 was received. Reference number 987654.") == ["ACME-12345", "987654"]


def test_manual_signal_with_declared_category_and_process_false(inbox):
    row, created = inbox.ingest(manual("Got a call: rejected", category="REJECTION", hints=AttributionHints()), process=False)
    assert created and row.status == SignalStatus.NEW.value and row.classification == {"declared": "REJECTION"}
    inbox.process(row)
    assert row.category == "REJECTION" and row.classification_source == "declared" and row.status == SignalStatus.UNMATCHED.value


def test_ingest_many_counts(inbox):
    counts = inbox.ingest_many([email_to_ingest(email("A", "Thank you for applying.", message_id="<a@x>")), email_to_ingest(email("A", "Thank you for applying.", message_id="<a@x>")), email_to_ingest(email("B", "hello", message_id="<b@x>"))])
    assert counts["ingested"] == 2 and counts["duplicates"] == 1 and counts["processed"] == 2
