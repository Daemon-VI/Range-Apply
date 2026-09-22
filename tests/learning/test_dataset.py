"""The learning dataset: joins, evidence weighting, temporal boundaries and
no future leakage, tenant isolation."""

from datetime import timedelta

from app.learning.dataset import build_dataset
from app.learning.models import FEATURE_VERSION, EvidenceQuality, OutcomeCompleteness
from tests.learning.conftest import T0, history, spec


def _row(rows, attempt):
    return next(r for r in rows if r.application_id == attempt.id)


def test_joins_every_feature(db_session, tenant_id):
    attempt = history(db_session, tenant_id, [spec(company="Join Co", title="Platform Engineer (Remote)", source="LEVER", fit_band="MEDIUM", fit_score=60, lane="AUTO", level="L2", cover_letter="LIGHT", role_family="platform", executor="PLAYWRIGHT_LOCAL", events=[("APPLICATION_RECEIVED", "STRONG", 1), ("INTERVIEW_REQUESTED", "STRONG", 4), ("REJECTED", "STRONG", 9)])])[0]
    rows = build_dataset(db_session, tenant_id)
    r = _row(rows, attempt)
    assert r.feature_version == FEATURE_VERSION and r.company == "Join Co" and r.company_key == "join" and r.title_key == "platform engineer"
    assert r.source == "LEVER" and r.fit_band == "MEDIUM" and r.fit_score == 60 and r.policy_version == 1 and r.gate_ruleset_version == "tier1-gates-v1"
    assert r.lane == "AUTO" and r.tailoring_level == "L2" and r.cover_letter_mode == "LIGHT" and r.role_family == "platform" and r.positioning_variant_id
    assert r.execution_method == "PLAYWRIGHT_LOCAL" and r.verification_status == "VERIFIED" and r.verified and not r.uncertain
    assert r.responded and r.interviewed and r.rejected and not r.assessed
    assert r.days_to_response == 1.0 and r.days_to_rejection == 9.0 and r.outcome_completeness is OutcomeCompleteness.TERMINAL
    assert r.evidence_quality is EvidenceQuality.STRONG and r.evidence_counts == {"STRONG": 3} and r.events_counted == 3


def test_evidence_weighting_respects_phase10_strength(db_session, tenant_id):
    weak, moderate, mixed, none = history(
        db_session,
        tenant_id,
        [
            spec(events=[("REJECTED", "WEAK", 3)]),
            spec(events=[("APPLICATION_RECEIVED", "MODERATE", 2)]),
            spec(events=[("APPLICATION_RECEIVED", "WEAK", 1), ("INTERVIEW_REQUESTED", "STRONG", 5)]),
            spec(events=[]),
        ],
    )
    rows = build_dataset(db_session, tenant_id)
    w, m, x, n = (_row(rows, a) for a in (weak, moderate, mixed, none))
    assert not w.responded and not w.rejected and w.evidence_quality is EvidenceQuality.NONE and w.events_below_threshold == 1 and w.evidence_counts == {"WEAK": 1}
    assert m.responded and m.evidence_quality is EvidenceQuality.MODERATE
    assert x.responded and x.interviewed and x.evidence_quality is EvidenceQuality.STRONG and x.events_below_threshold == 1 and x.evidence_counts == {"WEAK": 1, "STRONG": 1}
    assert n.evidence_quality is EvidenceQuality.NONE and n.outcome_completeness is OutcomeCompleteness.SUBMITTED_ONLY
    # exploratory: weak evidence may label only when the tenant lowers the threshold
    exploratory = build_dataset(db_session, tenant_id, minimum_evidence=EvidenceQuality.WEAK)
    assert _row(exploratory, weak).rejected and _row(exploratory, weak).evidence_quality is EvidenceQuality.WEAK
    strict = build_dataset(db_session, tenant_id, minimum_evidence=EvidenceQuality.STRONG)
    assert not _row(strict, moderate).responded and _row(strict, moderate).events_below_threshold == 1


def test_unknown_and_likely_are_not_verified(db_session, tenant_id):
    unknown, likely, verified = history(db_session, tenant_id, [spec(verification="UNKNOWN", run_status="UNKNOWN"), spec(verification="LIKELY", run_status="SUBMITTED"), spec()])
    rows = build_dataset(db_session, tenant_id)
    u, lk, v = (_row(rows, a) for a in (unknown, likely, verified))
    assert u.uncertain and not u.verified and u.outcome_completeness is OutcomeCompleteness.UNVERIFIED
    assert not lk.verified and not lk.uncertain and lk.verification_status == "LIKELY"
    assert v.verified and v.verification_status == "VERIFIED"


def test_no_future_leakage(db_session, tenant_id):
    """Monday's view cannot see Friday's rejection, nor an event observed later than it happened."""
    early, late_observed, future = history(
        db_session,
        tenant_id,
        [
            spec(events=[("APPLICATION_RECEIVED", "STRONG", 1), ("REJECTED", "STRONG", 4)]),
            spec(events=[("INTERVIEW_REQUESTED", "STRONG", 1, 6)]),  # happened on day 1, observed on day 6
            spec(submitted_offset_hours=24 * 30),  # submitted a month later
        ],
        spacing_hours=0,
    )
    monday = T0 + timedelta(days=2)
    rows = build_dataset(db_session, tenant_id, as_of=monday)
    assert {r.application_id for r in rows} == {early.id, late_observed.id}, "the future attempt does not exist yet"
    e = _row(rows, early)
    assert e.responded and not e.rejected and e.outcome_completeness is OutcomeCompleteness.PROGRESS and e.as_of == monday.replace(tzinfo=e.as_of.tzinfo)
    lo = _row(rows, late_observed)
    assert not lo.interviewed and lo.events_counted == 0, "known on day 6, not on day 2"
    friday = T0 + timedelta(days=5)
    rows = build_dataset(db_session, tenant_id, as_of=friday)
    assert _row(rows, early).rejected and _row(rows, early).days_to_rejection == 4.0
    assert not _row(rows, late_observed).interviewed
    rows = build_dataset(db_session, tenant_id, as_of=T0 + timedelta(days=7))
    assert _row(rows, late_observed).interviewed and _row(rows, late_observed).days_to_response == 1.0
    assert len(build_dataset(db_session, tenant_id, as_of=T0 + timedelta(days=40))) == 3


def test_window_and_retracted_events(db_session, tenant_id):
    old, recent = history(db_session, tenant_id, [spec(events=[("REJECTED", "STRONG", 1)]), spec(submitted_offset_hours=24 * 100, events=[("INTERVIEW_REQUESTED", "STRONG", 1)])], spacing_hours=0)
    as_of = T0 + timedelta(days=110)
    assert {r.application_id for r in build_dataset(db_session, tenant_id, as_of=as_of, window_days=30)} == {recent.id}
    assert {r.application_id for r in build_dataset(db_session, tenant_id, as_of=as_of)} == {old.id, recent.id}
    from app.signals.database.models import OutcomeEventRow

    event = db_session.query(OutcomeEventRow).filter(OutcomeEventRow.application_id == recent.id).one()
    event.retracted = True
    db_session.commit()
    assert not _row(build_dataset(db_session, tenant_id, as_of=as_of), recent).interviewed


def test_tenant_isolation(db_session, tenant_id, other_tenant_id):
    mine = history(db_session, tenant_id, [spec(company="Shared Co", events=[("INTERVIEW_REQUESTED", "STRONG", 1)])])[0]
    theirs = history(db_session, other_tenant_id, [spec(company="Shared Co", events=[("REJECTED", "STRONG", 1)])] * 3)
    rows = build_dataset(db_session, tenant_id)
    assert [r.application_id for r in rows] == [mine.id] and rows[0].interviewed
    others = build_dataset(db_session, other_tenant_id)
    assert {r.application_id for r in others} == {a.id for a in theirs} and all(r.rejected for r in others)


def test_empty_and_pre_execution_attempts(db_session, tenant_id, opportunities):
    from tests.scheduler.conftest import fake_attempt

    assert build_dataset(db_session, tenant_id) == []
    co = opportunities.make(company="Ready Co")
    from app.application.models import ApplicationStatus

    fake_attempt(db_session, tenant_id, co, status=ApplicationStatus.READY, submitted_days_ago=None)
    assert build_dataset(db_session, tenant_id) == [], "an attempt that was never executed is not an outcome"
