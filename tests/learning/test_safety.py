"""High-volume safety: learning orders and informs, never filters. Priority
stays deterministic and unchanged unless ordering is explicitly enabled;
caps, bands, thresholds, blocklists and eligibility are untouched; nothing
crosses tenants."""

from datetime import timedelta

from app.intelligence.database.models import JobMatchRow
from app.learning.engine import LearnedPrior, LearningEngine
from app.learning.models import ORDERING_VERSION, TenantLearningSettings
from app.pipeline.models import ApplicationPolicyUpdate
from app.pipeline.repository import OpportunityRepository, PolicyRepository
from app.pipeline.sync import sync_match
from tests.learning.conftest import T0, history, mixed_specs, spec


def _policy_snapshot(db, tenant_id):
    row = PolicyRepository(db, tenant_id).get_or_create()
    return {k: v for k, v in PolicyRepository.snapshot(row).items() if k not in ("learning_settings", "version")}


def test_learning_never_mutates_policy_caps_bands_or_blocklist(db_session, tenant_id, engine):
    before = _policy_snapshot(db_session, tenant_id)
    history(db_session, tenant_id, [spec(company="Bad Luck Inc", events=[("REJECTED", "STRONG", 1)])] * 30)
    engine.snapshot(T0 + timedelta(days=30), actor="test")
    engine.compute()
    db_session.commit()
    assert _policy_snapshot(db_session, tenant_id) == before
    policy = PolicyRepository(db_session, tenant_id).get()
    assert policy.blocked_companies == [] and policy.daily_cap == 50 and policy.weekly_cap == 300 and policy.minimum_fit_score is None and set(b.value for b in policy.enabled_bands) == {"HIGH", "MEDIUM", "LOW"}
    from app.career.database.models import EvidenceNodeRow

    assert db_session.query(EvidenceNodeRow).filter(EvidenceNodeRow.tenant_id == tenant_id).count() == 0


def test_scheduler_processes_every_admissible_opportunity_with_ordering_enabled(db_session, tenant_id, opportunities, scheduler, engine):
    # a poor history for one company, then many new eligible openings there and elsewhere
    history(db_session, tenant_id, [spec(company="Cold Co", events=[("REJECTED", "STRONG", 1)])] * 12)
    engine.snapshot(actor="test")
    PolicyRepository(db_session, tenant_id).update(ApplicationPolicyUpdate(learning_settings=TenantLearningSettings(ordering_enabled=True)), "test")
    db_session.commit()
    # distinct companies (Phase 5's company cool-down is a separate, pre-existing rule)
    cos = [opportunities.make(company="Cold Co" if i == 0 else f"Warm Co {i}", title=f"Engineer {i}", fit_score=80 - i) for i in range(12)]
    preview = scheduler.preview(window=500)
    admitted = {d.candidate_opportunity_id for d in preview.decisions if d.code.value == "ADMITTED"}
    assert admitted == {co.id for co in cos}, "every admissible opportunity is admitted; learning did not filter"
    run = scheduler.run(window=500)
    assert run.admitted == 12 and run.blocked == 0
    from app.application.database.models import ApplicationRow

    assert db_session.query(ApplicationRow).filter(ApplicationRow.tenant_id == tenant_id, ApplicationRow.candidate_opportunity_id.in_([c.id for c in cos])).count() == 12, "an attempt was reserved for every one, including the poor-history company"


def test_priority_is_unchanged_unless_ordering_is_enabled(db_session, tenant_id, opportunities, engine):
    warm = opportunities.make(company="Warm Co", title="Backend Engineer", fit_score=70)
    cold = opportunities.make(company="Cold Co", title="Backend Engineer", fit_score=70)
    # one source for both, so the company history is the only thing that differs
    history(db_session, tenant_id, [spec(company=warm.opportunity.company, source="GREENHOUSE", events=[("INTERVIEW_REQUESTED", "STRONG", 1)])] * 20 + [spec(company=cold.opportunity.company, source="GREENHOUSE", events=[("REJECTED", "STRONG", 1)])] * 20)
    snap = engine.snapshot(actor="test")
    db_session.commit()
    repo = OpportunityRepository(db_session, tenant_id)

    def priority_of(co):
        match = db_session.get(JobMatchRow, co.match_id)
        sync_match(db_session, tenant_id, match)
        db_session.commit()
        db_session.refresh(co)
        return co.priority_score, repo.latest_priority(co.id).components

    # default: identical deterministic priority, neutral learned_prior, no learned provenance
    (warm_score, warm_c), (cold_score, cold_c) = priority_of(warm), priority_of(cold)
    assert warm_c["learned_prior"] == 50.0 and cold_c["learned_prior"] == 50.0 and "learned" not in warm_c
    again, _ = priority_of(warm)
    assert again == warm_score, "deterministic: same inputs, same score"
    # opt in: the learned_prior component moves, the provenance is recorded, eligibility is untouched
    PolicyRepository(db_session, tenant_id).update(ApplicationPolicyUpdate(learning_settings=TenantLearningSettings(ordering_enabled=True)), "test")
    db_session.commit()
    (warm_on, warm_c2), (cold_on, cold_c2) = priority_of(warm), priority_of(cold)
    assert warm_c2["learned_prior"] > 50.0 > cold_c2["learned_prior"] and warm_on >= cold_on
    assert warm_c2["learned"]["snapshot_id"] == snap.id and warm_c2["learned"]["ordering_version"] == ORDERING_VERSION
    assert warm_c2["weights"] == warm_c["weights"], "the priority weights themselves never change"
    for co in (warm, cold):
        db_session.refresh(co)
        assert co.policy_admitted is True, "the learned signal touches order only"
    # opt out again: back to exactly the default behaviour
    PolicyRepository(db_session, tenant_id).update(ApplicationPolicyUpdate(learning_settings=TenantLearningSettings(ordering_enabled=False)), "test")
    db_session.commit()
    back, back_c = priority_of(cold)
    assert back == cold_score and back_c["learned_prior"] == 50.0


def test_learned_signal_is_bounded_neutral_without_data_and_never_a_gate(db_session, tenant_id, engine):
    off = engine.expected_response(source="GREENHOUSE", company="Any", title="Engineer", fit_band="HIGH")
    assert off.enabled is False and off.score == 50.0
    on = LearningEngine(db_session, tenant_id, TenantLearningSettings(ordering_enabled=True))
    assert on.expected_response(source="GREENHOUSE", company="Any", title="Engineer", fit_band="HIGH").score == 50.0, "no snapshot: neutral"
    history(db_session, tenant_id, mixed_specs(40))
    on.snapshot(actor="test")
    db_session.commit()
    for band in ("HIGH", "MEDIUM", "LOW", None):
        r = on.expected_response(source="ASHBY", company="Company 1", title="Data Engineer", fit_band=band)
        assert 0.0 <= r.score <= 100.0 and r.snapshot_id and r.ordering_version == ORDERING_VERSION and all("used" in c for c in r.components)
    unknown = on.expected_response(source="NEWSOURCE", company="Never Seen", title="Nothing", fit_band=None)
    assert unknown.score == 50.0 and "neutral" in unknown.explanation
    prior = LearnedPrior(db_session, tenant_id, TenantLearningSettings(ordering_enabled=False))
    assert prior.score(source="ASHBY", company="Company 1", title="Data Engineer", fit_band="HIGH") is None


def test_no_hidden_top_n_in_the_engine(db_session, tenant_id, engine):
    history(db_session, tenant_id, mixed_specs(50))
    result = engine.compute()
    text = " ".join(r.text for r in result.recommendations).lower()
    assert "only apply" not in text and "do not apply" not in text and "skip" not in text
    import inspect

    import app.learning.engine as eng

    source = inspect.getsource(eng)
    for forbidden in ("blocked_companies", "daily_cap", "weekly_cap", "enabled_bands", "minimum_fit_score", "band_thresholds", "policy_admitted", "evaluate_admission"):
        assert forbidden not in source, f"the engine must not read or write {forbidden}"


def test_tenant_isolation_of_learning(db_session, tenant_id, other_tenant_id, client):
    from app.api.deps import get_tenant_id
    from app.main import app
    from tests.conftest import AUTH_HEADERS

    history(db_session, tenant_id, [spec(company="Shared Co", events=[("INTERVIEW_REQUESTED", "STRONG", 1)])] * 10 + [spec(company="Mine Only", events=[("REJECTED", "STRONG", 1)])] * 10)
    history(db_session, other_tenant_id, [spec(company="Shared Co", events=[("REJECTED", "STRONG", 1)])] * 10 + [spec(company="Theirs Only", events=[("INTERVIEW_REQUESTED", "STRONG", 1)])] * 10)
    mine = LearningEngine(db_session, tenant_id, TenantLearningSettings(ordering_enabled=True), actor="test")
    theirs = LearningEngine(db_session, other_tenant_id, TenantLearningSettings(ordering_enabled=True), actor="test")
    my_snap, their_snap = mine.snapshot(actor="test"), theirs.snapshot(actor="test")
    db_session.commit()
    assert mine.compute().baseline.estimates["interview_rate"].raw_rate == 0.5 and theirs.compute().baseline.estimates["interview_rate"].raw_rate == 0.5
    assert client.get("/api/v1/learning/dataset").json()["total"] == 20
    assert mine.expected_response(source="GREENHOUSE", company="Shared Co", title="Backend Engineer", fit_band="HIGH").score > 50 > theirs.expected_response(source="GREENHOUSE", company="Shared Co", title="Backend Engineer", fit_band="HIGH").score
    assert mine.get_snapshot(their_snap.id) is None and theirs.get_snapshot(my_snap.id) is None
    assert [s.id for s in mine.list_snapshots()] == [my_snap.id]
    assert client.get(f"/api/v1/learning/snapshots/{their_snap.id}").status_code == 404
    app.dependency_overrides[get_tenant_id] = lambda: other_tenant_id
    assert client.get(f"/api/v1/learning/snapshots/{my_snap.id}").status_code == 404
    assert client.get("/api/v1/learning/dataset").json()["total"] == 20
    assert client.get("/api/v1/learning/snapshots/latest").json()["snapshot"]["id"] == their_snap.id
    # the other tenant cannot flip my ordering switch
    client.put("/api/v1/learning/settings", json={"ordering_enabled": True}, headers=AUTH_HEADERS)
    app.dependency_overrides[get_tenant_id] = lambda: tenant_id
    assert PolicyRepository(db_session, tenant_id).get().learning_settings.ordering_enabled is False
