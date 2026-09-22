"""Fit (Phase 3's score) and priority (processing order) are separate, persisted, deterministic."""

from datetime import datetime, timedelta, timezone

import pytest

from app.pipeline.models import FitBand
from app.pipeline.policy import band_for
from app.pipeline.priority import (
    DEFAULT_PRIORITY_WEIGHTS,
    PriorityInputs,
    compute_priority,
    deadline_component,
    freshness_component,
)
from app.pipeline.repository import OpportunityRepository

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


def test_band_thresholds_are_candidate_settings():
    assert band_for(85) is FitBand.HIGH
    assert band_for(70) is FitBand.MEDIUM  # boundary: HIGH is strictly above
    assert band_for(45) is FitBand.MEDIUM
    assert band_for(44) is FitBand.LOW
    assert band_for(None) is None
    assert band_for(60, {"HIGH": 55, "MEDIUM": 30}) is FitBand.HIGH


def test_freshness_and_deadline_components():
    assert freshness_component(PriorityInputs(80, posted_at=NOW - timedelta(days=1), now=NOW)) == 100.0
    assert freshness_component(PriorityInputs(80, posted_at=NOW - timedelta(days=40), now=NOW)) == 0.0
    mid = freshness_component(PriorityInputs(80, posted_at=NOW - timedelta(days=16), now=NOW))
    assert 0 < mid < 100
    assert freshness_component(PriorityInputs(80, now=NOW)) == 50.0
    assert deadline_component(PriorityInputs(80, deadline=NOW + timedelta(days=2), now=NOW)) == 100.0
    assert deadline_component(PriorityInputs(80, deadline=NOW + timedelta(days=30), now=NOW)) == 10.0
    assert deadline_component(PriorityInputs(80, deadline=NOW - timedelta(days=1), now=NOW)) == 0.0
    assert deadline_component(PriorityInputs(80, now=NOW)) == 20.0


def test_priority_is_deterministic_and_not_fit():
    fresh_85 = PriorityInputs(85, posted_at=NOW - timedelta(days=1), deadline=NOW + timedelta(days=2), source="GREENHOUSE", now=NOW)
    stale_92 = PriorityInputs(92, posted_at=NOW - timedelta(days=45), source="OTHER", now=NOW)
    a = compute_priority(fresh_85)
    b = compute_priority(stale_92)
    assert a.score > b.score, "a fresh, closing, easy 85 outranks a stale, hard 92"
    assert b.components["fit"] > a.components["fit"]
    assert compute_priority(fresh_85).score == a.score
    assert a.weights_version == "priority-v1"
    assert set(a.components) == set(DEFAULT_PRIORITY_WEIGHTS)
    assert abs(sum(a.weights.values()) - 1.0) < 1e-6


def test_custom_weights_change_order_but_never_admission():
    fresh_60 = PriorityInputs(60, posted_at=NOW - timedelta(days=1), now=NOW)
    stale_90 = PriorityInputs(90, posted_at=NOW - timedelta(days=45), now=NOW)
    fit_only = {"fit": 1.0, "freshness": 0.0, "deadline": 0.0, "source_reliability": 0.0, "execution_ease": 0.0, "learned_prior": 0.0}
    assert compute_priority(stale_90, fit_only).score > compute_priority(fresh_60, fit_only).score
    fresh_only = {"fit": 0.0, "freshness": 1.0, "deadline": 0.0, "source_reliability": 0.0, "execution_ease": 0.0, "learned_prior": 0.0}
    assert compute_priority(fresh_60, fresh_only).score > compute_priority(stale_90, fresh_only).score
    with pytest.raises(ValueError):
        compute_priority(fresh_60, {k: 0.0 for k in fit_only})


def test_fit_and_priority_persist_separately_and_order_listing(db_session, jobs, matches, repo):
    stale_high = jobs.make(title="Data Engineer", posted_days_ago=45, source="OTHER")
    fresh_mid = jobs.make(title="Backend Engineer", posted_days_ago=0.5, deadline_in_days=2)
    rows = {}
    for job, score in ((stale_high, 92), (fresh_mid, 80)):
        opp, _, _ = OpportunityRepository.resolve_opportunity(db_session, job)
        co, _ = repo.ensure_candidate_opportunity(opp, "test")
        match = matches.make(job, fit_score=score)
        repo.record_fit(co, match, band_for(score), "test")
        result = compute_priority(PriorityInputs(score, posted_at=job.posted_at.replace(tzinfo=timezone.utc), deadline=job.deadline.replace(tzinfo=timezone.utc) if job.deadline else None, source=job.source))
        repo.record_priority(co, result, "test")
        rows[job.id] = co
    repo.commit()

    high = rows[stale_high.id]
    mid = rows[fresh_mid.id]
    assert high.fit_score == 92 and high.fit_band == "HIGH" and repo.match_for(high).id
    assert mid.fit_score == 80 and mid.fit_band == "HIGH"
    assert mid.priority_score > high.priority_score
    assert repo.latest_priority(mid.id).weights_version == "priority-v1"
    assert "weights" in repo.latest_priority(mid.id).components

    listed, total = repo.list_candidate_opportunities()
    assert total == 2 and [co.id for co in listed] == [mid.id, high.id]

    actions = [e.action for e in repo.list_audit("candidate_opportunity", mid.id)]
    assert "fit" in actions and "priority" in actions
