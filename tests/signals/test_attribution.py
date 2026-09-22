"""Deterministic attribution: strong identifiers first, company/title evidence last,
ambiguity and conflicts become NEEDS_REVIEW, nothing crosses tenants."""

from app.application.models import ApplicationStatus
from app.signals.models import (
    ATTRIBUTION_VERSION,
    AttributionHints,
    AttributionStatus,
    SignalStatus,
)
from tests.signals.conftest import email, manual, submitted


def _attr(inbox, row):
    return [a for a in inbox.attributions_for(row.id) if a.id == row.attribution_id][0]


def test_exact_application_id(inbox, db_session, tenant_id, opportunities):
    attempt = submitted(db_session, tenant_id, opportunities)
    row, _ = inbox.ingest_email(email("Interview", "We'd like to invite you to an interview.", hints=AttributionHints(application_id=attempt.id)))
    a = _attr(inbox, row)
    assert row.attribution_status == "MATCHED" and row.application_id == attempt.id and a.rule == "application_id" and a.confidence == "HIGH" and a.attribution_version == ATTRIBUTION_VERSION
    assert a.evidence["rules"] == ["application_id"] and "identifies exactly one application" in a.explanation


def test_exact_opportunity_id_source_job_id_and_candidate_opportunity_id(inbox, db_session, tenant_id, opportunities):
    attempt = submitted(db_session, tenant_id, opportunities, company="Beta")
    for hints, rule in ((AttributionHints(opportunity_id=attempt.opportunity_id), "opportunity_id"), (AttributionHints(job_id=attempt.job_id), "source_job_id"), (AttributionHints(candidate_opportunity_id=attempt.candidate_opportunity_id), "candidate_opportunity_id")):
        row, _ = inbox.ingest_email(email("Interview", "We'd like to invite you to an interview.", message_id=f"<{rule}@x>", hints=hints))
        assert row.application_id == attempt.id and _attr(inbox, row).rule == rule, rule


def test_confirmation_reference_in_the_body(inbox, db_session, tenant_id, opportunities):
    attempt = submitted(db_session, tenant_id, opportunities, company="Gamma", reference="GAMMA-778899")
    submitted(db_session, tenant_id, opportunities, company="Delta", reference="DELTA-1")
    row, _ = inbox.ingest_email(email("Your application", "Thank you for applying. Application ID: GAMMA-778899", sender="no-reply@greenhouse-mail.io"))
    assert row.application_id == attempt.id and _attr(inbox, row).rule == "confirmation_reference" and _attr(inbox, row).evidence["confirmation_reference"]["reference"] == "GAMMA-778899"


def test_application_url_in_the_body(inbox, db_session, tenant_id, opportunities):
    attempt = submitted(db_session, tenant_id, opportunities, company="Epsilon", application_url="https://boards.greenhouse.io/epsilon/jobs/4242")
    row, _ = inbox.ingest_email(email("Application received", "We have received your application: https://boards.greenhouse.io/epsilon/jobs/4242?utm=1", sender="no-reply@greenhouse-mail.io"))
    assert row.application_id == attempt.id and _attr(inbox, row).rule == "application_url"


def test_company_and_title_evidence(inbox, db_session, tenant_id, opportunities):
    a = submitted(db_session, tenant_id, opportunities, company="Zeta Labs", title="Backend Engineer")
    b = submitted(db_session, tenant_id, opportunities, company="Zeta Labs", title="Data Engineer")
    row, _ = inbox.ingest_email(email(f"{a.company_name}: Backend Engineer", f"Thank you for applying to the Backend Engineer position at {a.company_name}.", sender="talent@zetalabs.com"))
    assert row.application_id == a.id and _attr(inbox, row).rule == "company_title" and _attr(inbox, row).confidence == "HIGH"
    # company only, several attempts: ambiguous, never a guess
    row2, _ = inbox.ingest_email(email("Zeta Labs", f"Thank you for applying to {a.company_name}.", sender="talent@zetalabs.com", message_id="<z2@x>"))
    assert row2.attribution_status == "AMBIGUOUS" and row2.status == SignalStatus.NEEDS_REVIEW.value and set(_attr(inbox, row2).candidates) == {a.id, b.id}


def test_company_only_with_exactly_one_attempt_is_a_medium_match(inbox, db_session, tenant_id, opportunities):
    attempt = submitted(db_session, tenant_id, opportunities, company="Theta")
    row, _ = inbox.ingest_email(email("Theta", f"Thank you for applying to {attempt.company_name}.", sender="hr@theta-other.io"))
    assert row.application_id == attempt.id and _attr(inbox, row).rule == "company_only" and _attr(inbox, row).confidence == "MEDIUM"


def test_sender_domain_is_evidence_but_ats_mailers_are_not(inbox, db_session, tenant_id, opportunities):
    attempt = submitted(db_session, tenant_id, opportunities, company="Iota")
    row, _ = inbox.ingest_email(email("Your application", "Thank you for applying!", sender="careers@iota.com"))
    assert row.application_id == attempt.id and list(_attr(inbox, row).evidence["company_evidence"].values()) == ["sender_domain"]
    row2, _ = inbox.ingest_email(email("Your application", "Thank you for applying!", sender="no-reply@greenhouse-mail.io", message_id="<gh@x>"))
    assert row2.attribution_status == "UNMATCHED" and row2.status == SignalStatus.UNMATCHED.value


def test_no_match_is_unmatched_and_explained(inbox, db_session, tenant_id, opportunities):
    submitted(db_session, tenant_id, opportunities, company="Kappa")
    row, _ = inbox.ingest_email(email("Hi", "Thank you for applying to Lambda.", sender="x@lambda.com"))
    a = _attr(inbox, row)
    assert row.status == SignalStatus.UNMATCHED.value and a.status == "UNMATCHED" and a.rule == "none" and "no identifier" in a.explanation


def test_conflicting_identifiers_need_review(inbox, db_session, tenant_id, opportunities):
    a = submitted(db_session, tenant_id, opportunities, company="Mu", reference="MU-1000")
    b = submitted(db_session, tenant_id, opportunities, company="Nu")
    row, _ = inbox.ingest_email(email("Confirmation", "Application ID: MU-1000", hints=AttributionHints(application_id=b.id)))
    attr = _attr(inbox, row)
    assert row.attribution_status == "AMBIGUOUS" and attr.rule == "conflicting_identifiers" and set(attr.candidates) == {a.id, b.id} and row.status == SignalStatus.NEEDS_REVIEW.value


def test_unknown_identifier_is_unmatched_not_guessed(inbox, db_session, tenant_id, opportunities):
    attempt = submitted(db_session, tenant_id, opportunities, company="Xi")
    row, _ = inbox.ingest_email(email("Xi", f"Thank you for applying to {attempt.company_name}.", hints=AttributionHints(application_id="00000000-0000-0000-0000-000000000000")))
    assert row.attribution_status == "UNMATCHED" and _attr(inbox, row).rule == "unknown_identifier"


def test_email_thread_follows_an_attributed_message(inbox, db_session, tenant_id, opportunities):
    attempt = submitted(db_session, tenant_id, opportunities, company="Omicron")
    inbox.ingest_email(email("Interview", "We'd like to invite you to an interview.", message_id="<t1@omicron.com>", hints=AttributionHints(application_id=attempt.id)))
    reply, _ = inbox.ingest_email(email("Re: Interview", "Please complete the coding challenge before we talk.", message_id="<t2@omicron.com>", headers={"In-Reply-To": "<t1@omicron.com>"}))
    assert reply.application_id == attempt.id and _attr(inbox, reply).rule == "email_thread"


def test_pre_execution_attempts_are_not_candidates_for_textual_evidence(inbox, db_session, tenant_id, opportunities):
    attempt = submitted(db_session, tenant_id, opportunities, company="Pi", status=ApplicationStatus.READY)
    row, _ = inbox.ingest_email(email("Pi", f"Thank you for applying to {attempt.company_name}.", sender="hr@pi-other.com"))
    assert row.attribution_status == "UNMATCHED" and _attr(inbox, row).rule == "company_no_attempt"


def test_tenant_isolation_of_attribution(inbox, other_inbox, db_session, tenant_id, other_tenant_id, opportunities, jobs, matches):
    from tests.preparation.conftest import OpportunityFactory

    mine = submitted(db_session, tenant_id, opportunities, company="Rho", reference="RHO-1")
    theirs = submitted(db_session, other_tenant_id, OpportunityFactory(db_session, other_tenant_id, jobs, matches), company="Rho", title="Platform Engineer", reference="RHO-2")
    # the other tenant's strong identifiers are invisible to me
    row, _ = inbox.ingest_email(email("Rho", "Application ID: RHO-2", hints=AttributionHints(application_id=theirs.id)))
    assert row.attribution_status == "UNMATCHED" and row.application_id is None
    row2, _ = inbox.ingest_email(email("Rho", f"Thank you for applying to {mine.company_name}.", sender="hr@rho-x.com", message_id="<r2@x>"))
    assert row2.application_id == mine.id, "company evidence only sees my own attempts"
    row3, _ = other_inbox.ingest_email(email("Rho", f"Thank you for applying to {theirs.company_name}.", sender="hr@rho-x.com", message_id="<r3@x>"))
    assert row3.application_id == theirs.id
    assert other_inbox.get_signal(row.id) is None and inbox.get_signal(row3.id) is None


def test_manual_signal_with_hints(inbox, db_session, tenant_id, opportunities):
    attempt = submitted(db_session, tenant_id, opportunities, company="Sigma")
    row, _ = inbox.ingest(manual("Recruiter called: interview next week", category="INTERVIEW_INVITATION", hints=AttributionHints(application_id=attempt.id)))
    assert row.application_id == attempt.id and row.status == SignalStatus.APPLIED.value and row.classification_source == "declared"


def test_attribution_history_is_append_only(inbox, db_session, tenant_id, opportunities):
    a = submitted(db_session, tenant_id, opportunities, company="Tau", title="Backend Engineer")
    row, _ = inbox.ingest_email(email("Tau", f"Thank you for applying to {a.company_name}.", sender="hr@tau-x.com"))
    assert row.application_id == a.id
    b = submitted(db_session, tenant_id, opportunities, company="Tau", title="Platform Engineer")
    inbox.reprocess(row.id, "test")
    history = inbox.attributions_for(row.id)
    assert [h.status for h in history] == ["MATCHED", "AMBIGUOUS"] and history[0].superseded_by_id == history[1].id and row.status == SignalStatus.NEEDS_REVIEW.value
    inbox.link(row.id, b.id, "reviewer")
    history = inbox.attributions_for(row.id)
    assert [h.status for h in history] == ["MATCHED", "AMBIGUOUS", "MANUAL"] and history[2].rule == "human_link" and history[2].actor == "reviewer" and row.application_id == b.id
    assert AttributionStatus(row.attribution_status) is AttributionStatus.MANUAL


def test_real_world_titles_with_punctuation_still_identify_the_application(inbox, db_session, tenant_id, opportunities):
    """Phase 13: real board titles read "Senior Product Manager - Subscriptions"
    or "Platform Engineer (AMER/APAC)"; the message quotes them verbatim."""
    a = submitted(db_session, tenant_id, opportunities, company="Spotify", title="Senior Product Manager - Subscriptions")
    submitted(db_session, tenant_id, opportunities, company="Spotify", title="Staff Data Scientist, Experience (Remote)")
    row, _ = inbox.ingest_email(email(f"Interview invitation: Senior Product Manager - Subscriptions at {a.company_name}", "We'd love to invite you to a phone screen.", sender="talent@spotify.example"))
    assert row.application_id == a.id and _attr(inbox, row).rule == "company_title"
    row2, _ = inbox.ingest_email(email(f"Your application to {a.company_name}", "Thanks for applying to the Staff Data Scientist, Experience (Remote) role.", sender="no-reply@spotify.example"))
    assert row2.application_id is not None and row2.application_id != a.id
