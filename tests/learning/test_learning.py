"""Learned metrics per dimension, response times, snapshots and versioning,
recommendations with their evidence."""

from datetime import timedelta

from app.learning.engine import LearningEngine
from app.learning.models import (
    FEATURE_VERSION,
    LEARNING_VERSION,
    SMOOTHING_METHOD,
    Dimension,
    LearningConfidence,
    Metric,
    TenantLearningSettings,
)
from app.pipeline.repository import OpportunityRepository
from tests.learning.conftest import T0, history, mixed_specs, spec


def _group(result, dimension, key):
    return next(g for g in result.groups if g.dimension is dimension and g.group_key == key)


def test_metrics_per_dimension_with_counts_and_baseline(db_session, tenant_id, engine):
    history(db_session, tenant_id, mixed_specs(60))
    result = engine.compute(T0 + timedelta(days=60))
    assert result.dataset_size == 60 and result.learning_version == LEARNING_VERSION and result.feature_version == FEATURE_VERSION and result.smoothing_method == SMOOTHING_METHOD
    assert result.outcome_rules_version == "outcome-rules-v1" and result.attribution_version == "signal-attribution-v1"
    base = result.baseline
    # per 10: 3 received+rejected, 1 received+interview, 1 assessment, 1 MODERATE received, 1 WEAK rejection (ignored), 3 nothing
    assert base.n == 60 and base.estimates["response_rate"].positives == 36 and base.estimates["interview_rate"].positives == 6 and base.estimates["rejection_rate"].positives == 18 and base.estimates["assessment_rate"].positives == 6
    assert base.estimates["response_rate"].raw_rate == 0.6 and base.estimates["response_rate"].smoothed_rate == 0.6, "the baseline is the reference, not shrunk"
    assert base.evidence == {"STRONG": 30, "MODERATE": 6, "NONE": 24}, "weak-only rows carry no evidence for the labels"
    assert base.estimates["uncertainty_rate"].positives == 5 and base.estimates["verified_submission_rate"].positives == 47
    dims = {g.dimension for g in result.groups}
    assert dims >= {Dimension.SOURCE, Dimension.COMPANY, Dimension.TITLE, Dimension.ROLE_FAMILY, Dimension.FIT_BAND, Dimension.LANE, Dimension.TAILORING_LEVEL, Dimension.COVER_LETTER_MODE, Dimension.POSITIONING_VARIANT, Dimension.EXECUTION_METHOD}
    greenhouse = _group(result, Dimension.SOURCE, "GREENHOUSE")
    assert greenhouse.n == 20 and greenhouse.confidence is LearningConfidence.HIGH and greenhouse.estimates["response_rate"].baseline_rate == 0.6
    backend = _group(result, Dimension.ROLE_FAMILY, "backend")
    assert backend.n == 30 and _group(result, Dimension.TITLE, "backend engineer").n == 30, "titles are grouped by the existing normalization, not by similar words"
    company0 = _group(result, Dimension.COMPANY, "company 0")
    assert company0.n == 6 and company0.group_label == "Company 0" and company0.confidence is LearningConfidence.MEDIUM
    for g in result.groups:
        for e in g.estimates.values():
            assert e.n == g.n and e.positives + e.negatives == e.n and 0.0 <= e.ci_low <= e.ci_high <= 1.0 and 0.0 <= e.smoothed_rate <= 1.0
    assert result.summary["ai_calls"] == 0 and result.summary["groups"] == len(result.groups)


def test_response_time_statistics(db_session, tenant_id, engine):
    history(db_session, tenant_id, [spec(events=[("APPLICATION_RECEIVED", "STRONG", 1), ("REJECTED", "STRONG", 10)]), spec(events=[("INTERVIEW_REQUESTED", "STRONG", 3)]), spec(events=[("REJECTED", "STRONG", 20)]), spec()])
    result = engine.compute(T0 + timedelta(days=40))
    assert result.baseline.medians["days_to_response"] == 3.0 and result.baseline.median_samples["days_to_response"] == 3
    assert result.baseline.medians["days_to_rejection"] == 15.0 and result.baseline.median_samples["days_to_rejection"] == 2
    assert result.summary["median_days_to_response"] == 3.0


def test_sparse_groups_are_not_authoritative(db_session, tenant_id, engine):
    history(db_session, tenant_id, [spec(company="Lucky Co", events=[("INTERVIEW_REQUESTED", "STRONG", 2)])] + [spec(company=f"Other {i}", events=[("REJECTED", "STRONG", 2)]) for i in range(9)])
    result = engine.compute(T0 + timedelta(days=30))
    lucky = _group(result, Dimension.COMPANY, "lucky")
    e = lucky.estimates["interview_rate"]
    assert e.raw_rate == 1.0 and e.smoothed_rate < 0.2 and lucky.confidence is LearningConfidence.LOW and e.ci_low < 0.25
    kinds = {(r.kind, r.group_key) for r in result.recommendations}
    assert ("INSUFFICIENT_DATA", "lucky") in kinds and not any(k == "HIGHER_INTERVIEW" for k, _ in kinds), "one interview from one application proves nothing"


def test_recommendations_carry_evidence_and_are_associational(db_session, tenant_id, engine):
    specs = [spec(source="GREENHOUSE", events=[("INTERVIEW_REQUESTED", "STRONG", 2)]) for _ in range(20)] + [spec(source="LEVER", events=[("REJECTED", "STRONG", 2)] if i % 4 == 0 else []) for i in range(20)]
    history(db_session, tenant_id, specs)
    result = engine.compute(T0 + timedelta(days=30))
    higher = [r for r in result.recommendations if r.kind == "HIGHER_RESPONSE" and r.group_key == "GREENHOUSE"]
    lower = [r for r in result.recommendations if r.kind == "LOWER_RESPONSE" and r.group_key == "LEVER"]
    assert higher and lower
    r = higher[0]
    assert r.n == 20 and r.positives == 20 and r.observed_rate == 1.0 and r.baseline_rate == 0.625 and r.confidence is LearningConfidence.HIGH
    assert r.learning_version == LEARNING_VERSION and r.as_of == result.as_of and r.window_days is None and r.evidence == {"STRONG": 20}
    assert "Historically" in r.text and "observed" in r.text and "not a causal claim" in r.caveat
    assert not any(word in r.text.lower() for word in ("would have", "causes", "because of"))


def test_snapshots_are_persisted_versioned_and_immutable(db_session, tenant_id, engine):
    history(db_session, tenant_id, mixed_specs(30))
    first = engine.snapshot(T0 + timedelta(days=5), actor="test")
    later = engine.snapshot(T0 + timedelta(days=60), actor="test")
    db_session.commit()
    assert first.id != later.id and first.dataset_size < later.dataset_size and later.dataset_size == 30
    assert first.learning_version == LEARNING_VERSION and first.feature_version == FEATURE_VERSION and first.smoothing_method == SMOOTHING_METHOD and first.outcome_rules_version == "outcome-rules-v1" and first.attribution_version == "signal-attribution-v1"
    assert first.window_days is None and first.generated_at is not None and first.metric_count > 0 and first.settings["min_samples"] == 5
    early_metrics = {(m.dimension, m.group_key, m.metric): m for m in engine.metrics(first.id)}
    assert early_metrics[("GLOBAL", "all", "response_rate")].n == first.dataset_size
    # what CareerOS believed at day 5 stays answerable after day 60's snapshot
    history(db_session, tenant_id, [spec(company="Late Co", events=[("REJECTED", "STRONG", 1)])], start=T0 + timedelta(days=70))
    engine.snapshot(T0 + timedelta(days=80), actor="test")
    db_session.commit()
    again = {(m.dimension, m.group_key, m.metric): m for m in engine.metrics(first.id)}
    assert {k: (v.n, v.positives, v.smoothed_rate) for k, v in again.items()} == {k: (v.n, v.positives, v.smoothed_rate) for k, v in early_metrics.items()}
    assert [s.id for s in engine.list_snapshots()][0] == engine.latest_snapshot().id
    audit = OpportunityRepository(db_session, tenant_id).list_audit("learning_snapshot", first.id)
    assert audit[0].action == "generated" and audit[0].after["ai_calls"] == 0 and audit[0].after["learning_version"] == LEARNING_VERSION


def test_methodology_change_is_a_new_version_not_an_overwrite(db_session, tenant_id, monkeypatch):
    history(db_session, tenant_id, mixed_specs(20))
    a = LearningEngine(db_session, tenant_id, TenantLearningSettings(prior_strength=10), actor="test").snapshot(T0 + timedelta(days=30))
    b = LearningEngine(db_session, tenant_id, TenantLearningSettings(prior_strength=2), actor="test").snapshot(T0 + timedelta(days=30))
    db_session.commit()
    ma = {(m.dimension, m.group_key): m for m in LearningEngine(db_session, tenant_id).metrics(a.id, Dimension.SOURCE, Metric.RESPONSE_RATE)}
    mb = {(m.dimension, m.group_key): m for m in LearningEngine(db_session, tenant_id).metrics(b.id, Dimension.SOURCE, Metric.RESPONSE_RATE)}
    assert a.settings["prior_strength"] == 10 and b.settings["prior_strength"] == 2
    assert any(ma[k].smoothed_rate != mb[k].smoothed_rate for k in ma) and all(ma[k].raw_rate == mb[k].raw_rate for k in ma)
    monkeypatch.setattr(LearningEngine, "VERSION", "learning-v2-test")
    c = LearningEngine(db_session, tenant_id, TenantLearningSettings(), actor="test").snapshot(T0 + timedelta(days=30))
    db_session.commit()
    assert c.learning_version == "learning-v2-test" and db_session.get(type(a), a.id).learning_version == LEARNING_VERSION


def test_window_setting_is_recorded_on_the_snapshot(db_session, tenant_id):
    history(db_session, tenant_id, [spec(events=[("REJECTED", "STRONG", 1)]), spec(submitted_offset_hours=24 * 50, events=[("INTERVIEW_REQUESTED", "STRONG", 1)])], spacing_hours=0)
    engine = LearningEngine(db_session, tenant_id, TenantLearningSettings(window_days=30), actor="test")
    snap = engine.snapshot(T0 + timedelta(days=55))
    assert snap.window_days == 30 and snap.window_start is not None and snap.dataset_size == 1 and snap.summary["interviewed"] == 1


def test_source_discovery_is_job_side_information(db_session, tenant_id, engine):
    history(db_session, tenant_id, mixed_specs(12))
    rows = engine.source_discovery(T0 + timedelta(days=30))
    by_source = {r["source"]: r for r in rows}
    assert by_source["GREENHOUSE"]["jobs_seen"] >= 4 and by_source["GREENHOUSE"]["jobs_closed"] == 0 and "no candidate outcomes" in by_source["GREENHOUSE"]["scope"]
