"""Human review (confirm / reject / relink / ignore / merge, all audited), the
/api/v1/signals surface and the dashboard pages; cross-tenant access refused."""

from app.application.models import ApplicationStatus
from app.pipeline.repository import OpportunityRepository
from app.signals.models import AttributionHints, OutcomeKind, SignalCategory, SignalStatus
from tests.conftest import AUTH_HEADERS
from tests.signals.conftest import email, submitted


def test_review_actions_are_auditable_and_reversible(inbox, db_session, tenant_id, opportunities):
    a = submitted(db_session, tenant_id, opportunities, company="Rev A", title="Backend Engineer")
    b = submitted(db_session, tenant_id, opportunities, company="Rev A", title="Data Engineer")
    row, _ = inbox.ingest_email(email("Rev A", f"Thank you for applying to {a.company_name}. We'd like to invite you to an interview.", sender="hr@reva.com"))
    assert row.status == SignalStatus.NEEDS_REVIEW.value and row.attribution_status == "AMBIGUOUS"
    # relink: a person picks the application
    inbox.link(row.id, b.id, "reviewer", "it was the data role")
    assert row.application_id == b.id and row.status == SignalStatus.APPLIED.value
    db_session.refresh(b)
    assert b.status == ApplicationStatus.INTERVIEWING.value and inbox.outcome_history(b.id)[0].current_outcome == "INTERVIEW_REQUESTED"
    # relink again: the first application's events are retracted, never deleted
    inbox.link(row.id, a.id, "reviewer", "no, backend")
    current_b, events_b = inbox.outcome_history(b.id)
    assert events_b[0].retracted and "relinked" in events_b[0].retracted_reason and current_b.current_outcome == "UNKNOWN" and current_b.event_count == 0
    assert inbox.outcome_history(a.id)[0].current_outcome == "INTERVIEW_REQUESTED"
    # reject the classification: outcome retracted, signal back in review
    inbox.reject_classification(row.id, "reviewer", "it was just a newsletter")
    assert row.status == SignalStatus.NEEDS_REVIEW.value and row.category == "UNKNOWN" and inbox.outcome_history(a.id)[0].current_outcome == "UNKNOWN"
    # confirm a classification: a STRONG human event
    inbox.confirm_classification(row.id, SignalCategory.ASSESSMENT, "reviewer")
    current_a, events_a = inbox.outcome_history(a.id)
    assert current_a.current_outcome == "ASSESSMENT_REQUESTED" and events_a[-1].origin == "human" and events_a[-1].evidence == "STRONG" and row.status == SignalStatus.APPLIED.value
    # ignore: everything from this signal retracted
    inbox.ignore(row.id, "reviewer", "not relevant after all")
    assert row.status == SignalStatus.IGNORED.value and inbox.outcome_history(a.id)[0].current_outcome == "UNKNOWN"
    actions = [e.action for e in OpportunityRepository(db_session, tenant_id).list_audit("signal", row.id)]
    for expected in ("ingested", "processed", "attributed", "review:link", "review:reject_classification", "review:confirm_classification", "review:ignore"):
        assert expected in actions, expected
    history = inbox.attributions_for(row.id)
    assert [h.status for h in history] == ["AMBIGUOUS", "MANUAL", "MANUAL"] and all(h.superseded_by_id for h in history[:-1])


def test_merge_duplicate_signal(inbox, db_session, tenant_id, opportunities):
    attempt = submitted(db_session, tenant_id, opportunities, company="Merge Co")
    hints = AttributionHints(application_id=attempt.id)
    a, _ = inbox.ingest_email(email("Interview", "We'd like to invite you to an interview.", message_id="<m1@x>", hints=hints))
    b, _ = inbox.ingest_email(email("Fwd: Interview", "FYI -- We'd like to invite you to an interview.", message_id="<m2@x>", hints=hints))
    assert len(inbox.outcome_history(attempt.id)[1]) == 2
    inbox.merge(b.id, a.id, "reviewer")
    assert b.status == SignalStatus.MERGED.value and b.merged_into_id == a.id and a.observation_count == 2
    current, events = inbox.outcome_history(attempt.id)
    assert current.event_count == 1 and [e.retracted for e in events] == [False, True]
    assert inbox.observations_for(a.id)[-1].provenance["merged_from"] == b.id


def test_purge_excerpts_keeps_metadata(inbox, db_session, tenant_id, opportunities):
    from datetime import timedelta

    attempt = submitted(db_session, tenant_id, opportunities, company="Purge Co")
    row, _ = inbox.ingest_email(email("Interview", "We'd like to invite you to an interview.", hints=AttributionHints(application_id=attempt.id)))
    row.observed_at = row.observed_at - timedelta(days=400)
    db_session.flush()
    assert inbox.purge_excerpts(180) == 1 and row.excerpt is None and row.content_hash and row.payload.get("excerpt_purged") is True and row.category == "INTERVIEW_INVITATION"


# ----------------------------------------------------------------- API


def test_api_ingest_list_trace_and_review(client, db_session, tenant_id, opportunities):
    a = submitted(db_session, tenant_id, opportunities, company="Api Co", title="Backend Engineer")
    b = submitted(db_session, tenant_id, opportunities, company="Api Co", title="Data Engineer")
    db_session.commit()
    body = {"message_id": "<api1@x>", "sender": "hr@apico.com", "subject": "Interview", "text": f"Thank you for applying to {a.company_name}. We'd like to invite you to an interview.", "headers": {"Authorization": "Bearer nope"}}
    assert client.post("/api/v1/signals/email", json=body).status_code == 401
    created = client.post("/api/v1/signals/email", json=body, headers=AUTH_HEADERS).json()
    assert created["created"] is True and created["signal"]["status"] == "NEEDS_REVIEW" and created["signal"]["attribution_status"] == "AMBIGUOUS"
    again = client.post("/api/v1/signals/email", json=body, headers=AUTH_HEADERS).json()
    assert again["created"] is False and again["signal"]["observation_count"] == 2
    sid = created["signal"]["id"]
    assert "nope" not in client.get(f"/api/v1/signals/{sid}").text
    review = client.get("/api/v1/signals/review").json()
    assert [s["id"] for s in review["items"]] == [sid]
    listed = client.get("/api/v1/signals", params={"status": "NEEDS_REVIEW", "source": "EMAIL", "category": "INTERVIEW_INVITATION", "days": 1}).json()
    assert listed["total"] == 1
    linked = client.post(f"/api/v1/signals/{sid}/link", json={"application_id": b.id, "note": "data role"}, headers=AUTH_HEADERS).json()
    assert linked["application_id"] == b.id and linked["status"] == "APPLIED"
    trace = client.get(f"/api/v1/signals/{sid}/trace").json()
    assert trace["attempt"]["id"] == b.id and trace["opportunity"]["company"] == b.company_name and trace["application_outcome"]["current_outcome"] == "INTERVIEW_REQUESTED" and len(trace["attributions"]) == 2
    history = client.get(f"/api/v1/signals/outcomes/{b.id}").json()
    assert history["current"]["current_outcome"] == "INTERVIEW_REQUESTED" and history["events"][0]["evidence"] == "STRONG"
    outcomes = client.get("/api/v1/signals/outcomes", params={"current": "INTERVIEW_REQUESTED"}).json()
    assert outcomes["total"] == 1 and outcomes["items"][0]["application_id"] == b.id
    by_company = client.get("/api/v1/signals", params={"company": "Api Co", "outcome": "INTERVIEW_REQUESTED"}).json()
    assert by_company["total"] == 1
    confirmed = client.post(f"/api/v1/signals/{sid}/confirm-outcome", json={"outcome": "INTERVIEW_SCHEDULED"}, headers=AUTH_HEADERS).json()
    assert confirmed["status"] == "APPLIED" and client.get(f"/api/v1/signals/outcomes/{b.id}").json()["current"]["current_outcome"] == "INTERVIEW_SCHEDULED"
    rejected = client.post(f"/api/v1/signals/{sid}/reject-classification", json={"note": "wrong"}, headers=AUTH_HEADERS).json()
    assert rejected["status"] == "NEEDS_REVIEW"
    reclassified = client.post(f"/api/v1/signals/{sid}/confirm-classification", json={"category": "INTERVIEW_INVITATION"}, headers=AUTH_HEADERS).json()
    assert reclassified["status"] == "APPLIED"
    assert client.post(f"/api/v1/signals/{sid}/classify", headers=AUTH_HEADERS).status_code == 200
    ignored = client.post(f"/api/v1/signals/{sid}/ignore", json={"note": "done"}, headers=AUTH_HEADERS).json()
    assert ignored["status"] == "IGNORED"
    summary = client.get("/api/v1/signals/summary").json()
    assert summary["by_status"]["IGNORED"] == 1 and summary["tenant_id"] == tenant_id
    manual = client.post("/api/v1/signals", json={"source": "MANUAL", "text": "Recruiter called, interview Friday", "category": "INTERVIEW_INVITATION", "hints": {"application_id": a.id}}, headers=AUTH_HEADERS).json()
    assert manual["signal"]["status"] == "APPLIED" and manual["signal"]["classification_source"] == "declared"
    batch = client.post("/api/v1/signals/batch", json=[{"source": "STATUS_PAGE", "source_reference": "obs-9", "subject": "Status", "text": "Your application is under review.", "hints": {"application_id": a.id}}], headers=AUTH_HEADERS).json()
    assert batch["counts"]["ingested"] == 1
    assert client.post("/api/v1/signals", json={"source": "EMAIL", "text": "x", "category": "REJECTION"}, headers=AUTH_HEADERS).status_code == 422
    assert client.post("/api/v1/signals/purge-excerpts", headers=AUTH_HEADERS).json()["purged"] == 0


def test_api_and_dashboard_are_tenant_scoped(client, db_session, tenant_id, other_tenant_id, opportunities):
    from app.api.deps import get_tenant_id
    from app.main import app

    attempt = submitted(db_session, tenant_id, opportunities, company="Scope Co")
    db_session.commit()
    created = client.post("/api/v1/signals/email", json={"sender": "hr@scope.com", "subject": "Interview", "text": "We'd like to invite you to an interview.", "hints": {"application_id": attempt.id}}, headers=AUTH_HEADERS).json()
    sid = created["signal"]["id"]
    app.dependency_overrides[get_tenant_id] = lambda: other_tenant_id
    assert client.get(f"/api/v1/signals/{sid}").status_code == 404
    assert client.get(f"/api/v1/signals/{sid}/trace").status_code == 404
    assert client.post(f"/api/v1/signals/{sid}/ignore", headers=AUTH_HEADERS).status_code == 404
    assert client.get(f"/api/v1/signals/outcomes/{attempt.id}").status_code == 404
    assert client.get("/api/v1/signals").json()["total"] == 0
    # the other tenant cannot link its signal to my application either
    theirs = client.post("/api/v1/signals/email", json={"sender": "hr@scope.com", "subject": "Interview", "text": "We'd like to invite you to an interview.", "hints": {"application_id": attempt.id}}, headers=AUTH_HEADERS).json()
    assert theirs["signal"]["attribution_status"] == "UNMATCHED"
    assert client.post(f"/api/v1/signals/{theirs['signal']['id']}/link", json={"application_id": attempt.id}, headers=AUTH_HEADERS).status_code == 404
    app.dependency_overrides[get_tenant_id] = lambda: tenant_id
    assert client.get("/api/v1/signals").json()["total"] == 1


def test_dashboard_pages_and_actions(client, db_session, tenant_id, opportunities):
    from tests.conftest import TEST_API_KEY

    attempt = submitted(db_session, tenant_id, opportunities, company="Dash Co")
    db_session.commit()
    created = client.post("/api/v1/signals/email", json={"sender": "hr@dash.com", "subject": "Hello there", "text": "Just checking the weather.", "hints": {"application_id": attempt.id}}, headers=AUTH_HEADERS).json()
    sid = created["signal"]["id"]
    assert client.get("/dashboard/signals").status_code == 401
    client.get("/dashboard/", params={"key": TEST_API_KEY})
    page = client.get("/dashboard/signals", params={"review": "1"}).text
    assert "Hello there" in page and "NEEDS_REVIEW" in page and "review queue" in page
    detail = client.get(f"/dashboard/signals/{sid}").text
    assert "Trace" in detail and "Review actions" in detail and "Attribution history" in detail and attempt.id in detail
    moved = client.post(f"/dashboard/signals/{sid}/confirm-classification", data={"category": "INTERVIEW_INVITATION"})
    assert moved.status_code == 303 and "classification-confirmed" in moved.headers["location"]
    detail = client.get(f"/dashboard/signals/{sid}").text
    assert "INTERVIEW_REQUESTED" in detail and "APPLIED" in detail
    bad = client.post(f"/dashboard/signals/{sid}/merge", data={"into_signal_id": sid})
    assert bad.status_code == 303 and "error:" in bad.headers["location"]
    assert client.get("/dashboard/signals", params={"company": "Dash", "outcome": "INTERVIEW_REQUESTED"}).status_code == 200
    assert OutcomeKind.INTERVIEW_REQUESTED.value in client.get("/dashboard/signals").text
