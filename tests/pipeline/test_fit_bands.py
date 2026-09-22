"""Fit bands as versioned settings: boundaries, configuration, versioning,
historical immutability, policy interaction, API."""

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_tenant_id
from app.core.errors import ValidationFailed
from app.main import app
from app.pipeline.models import ApplicationPolicyUpdate, FitBand
from app.pipeline.policy import band_for, fit_band_config
from app.pipeline.sync import sync_match
from tests.conftest import AUTH_HEADERS


@pytest.mark.parametrize("score, band", [(0, FitBand.LOW), (44, FitBand.LOW), (45, FitBand.MEDIUM), (70, FitBand.MEDIUM), (71, FitBand.HIGH), (100, FitBand.HIGH)])
def test_default_boundaries_are_explicit(score, band):
    assert band_for(score) is band
    assert band_for(None) is None


def test_custom_thresholds_move_the_boundaries_only():
    thresholds = {"HIGH": 80, "MEDIUM": 60}
    assert band_for(80, thresholds) is FitBand.MEDIUM and band_for(81, thresholds) is FitBand.HIGH
    assert band_for(59, thresholds) is FitBand.LOW and band_for(60, thresholds) is FitBand.MEDIUM


def test_fit_band_config_is_a_versioned_view_of_the_policy(policy_repo):
    policy = policy_repo.get()
    config = fit_band_config(policy)
    assert config.version == 1 and config.thresholds == {"HIGH": 70, "MEDIUM": 45}
    ranges = {r.band: (r.min_score, r.max_score, r.enabled) for r in config.bands}
    assert ranges == {FitBand.HIGH: (71, 100, True), FitBand.MEDIUM: (45, 70, True), FitBand.LOW: (0, 44, True)}
    updated = policy_repo.update(ApplicationPolicyUpdate(band_thresholds={"HIGH": 80, "MEDIUM": 50}, enabled_bands=[FitBand.HIGH, FitBand.MEDIUM]), "test")
    config = fit_band_config(updated)
    assert config.version == 2 and {r.band: (r.min_score, r.max_score, r.enabled) for r in config.bands}[FitBand.LOW] == (0, 49, False)
    with pytest.raises(ValidationFailed):
        policy_repo.update(ApplicationPolicyUpdate(band_thresholds={"HIGH": 50, "MEDIUM": 50}), "test")


def test_threshold_change_never_rewrites_stored_matches_or_past_decisions(db_session, jobs, matches, tenant_id, repo, policy_repo):
    job = jobs.make(title="Band Engineer")
    match = matches.make(job, fit_score=72)
    _, result = sync_match(db_session, tenant_id, match)
    db_session.commit()
    co = result.candidate_opportunity
    assert co.fit_band == "HIGH" and co.fit_policy_version == 1 and co.admission_policy_version == 1
    fit_events_before = [e for e in repo.list_audit("candidate_opportunity", co.id) if e.action == "fit"]

    policy_repo.update(ApplicationPolicyUpdate(band_thresholds={"HIGH": 80, "MEDIUM": 45}), "test")
    db_session.commit()
    db_session.refresh(match)
    db_session.refresh(co)
    assert match.fit_score == 72, "the stored match is immutable"
    assert co.fit_band == "HIGH" and co.fit_policy_version == 1, "no re-banding until the next sync"

    sync_match(db_session, tenant_id, match)
    db_session.commit()
    db_session.refresh(co)
    assert co.fit_band == "MEDIUM" and co.fit_policy_version == 2 and match.fit_score == 72
    events = [e for e in repo.list_audit("candidate_opportunity", co.id) if e.action == "fit"]
    assert len(events) == len(fit_events_before) + 1
    assert events[0].before["fit_band"] == "HIGH" and events[0].before["fit_policy_version"] == 1 and events[0].after["fit_policy_version"] == 2
    assert fit_events_before[0].after == events[-1].after, "the earlier event is untouched"


def test_disabling_a_band_is_the_policys_decision_not_the_scorers(db_session, jobs, matches, tenant_id, policy_repo):
    job = jobs.make(title="Low Band Engineer")
    match = matches.make(job, fit_score=20)
    _, result = sync_match(db_session, tenant_id, match)
    assert result.admitted and result.candidate_opportunity.fit_band == "LOW"
    policy_repo.update(ApplicationPolicyUpdate(enabled_bands=[FitBand.HIGH, FitBand.MEDIUM]), "test")
    db_session.commit()
    _, result = sync_match(db_session, tenant_id, match)
    db_session.commit()
    co = result.candidate_opportunity
    assert not result.admitted and co.policy_reason == "band_disabled:LOW" and co.fit_band == "LOW" and co.admission_policy_version == 2


@pytest.fixture
def client(tenant_id):
    previous = app.dependency_overrides.get(get_tenant_id)
    app.dependency_overrides[get_tenant_id] = lambda: tenant_id
    try:
        yield TestClient(app)
    finally:
        if previous is not None:
            app.dependency_overrides[get_tenant_id] = previous
        else:
            app.dependency_overrides.pop(get_tenant_id, None)


def test_fit_band_and_gate_api(client, tenant_id, db_session, repo):
    config = client.get("/api/v1/policy/fit-bands").json()
    assert config["version"] == 1 and config["thresholds"] == {"HIGH": 70, "MEDIUM": 45} and len(config["bands"]) == 3
    assert client.put("/api/v1/policy/fit-bands", json={"thresholds": {"HIGH": 75, "MEDIUM": 40}}).status_code == 401
    updated = client.put("/api/v1/policy/fit-bands", json={"thresholds": {"HIGH": 75, "MEDIUM": 40}, "enabled_bands": ["HIGH", "MEDIUM"], "minimum_fit_score": 35}, headers=AUTH_HEADERS).json()
    assert updated["version"] == 2 and updated["thresholds"] == {"HIGH": 75, "MEDIUM": 40} and updated["minimum_fit_score"] == 35
    assert [b["enabled"] for b in updated["bands"]] == [True, True, False]
    cleared = client.put("/api/v1/policy/fit-bands", json={"clear_minimum_fit_score": True}, headers=AUTH_HEADERS).json()
    assert cleared["minimum_fit_score"] is None and cleared["version"] == 3
    bad = client.put("/api/v1/policy/fit-bands", json={"thresholds": {"HIGH": 30, "MEDIUM": 40}}, headers=AUTH_HEADERS)
    assert bad.status_code == 422
    gates = client.get("/api/v1/policy/gates").json()
    assert gates["ruleset_version"] == "tier1-gates-v3" and gates["policy_version"] == 3
    by_gate = {g["gate"]: g for g in gates["gates"]}
    assert by_gate["BAND_ENABLED"]["value"] == ["HIGH", "MEDIUM"] and by_gate["FIT_FLOOR"]["value"] is None
    events = repo.list_audit("application_policy")
    assert [e.action for e in events] == ["updated", "updated", "created"], "every change is audited with before/after"
    assert events[1].after["band_thresholds"] == {"HIGH": 75, "MEDIUM": 40} and events[1].before["band_thresholds"] == {"HIGH": 70, "MEDIUM": 45}
    assert events[0].before["minimum_fit_score"] == 35 and events[0].after["minimum_fit_score"] is None
