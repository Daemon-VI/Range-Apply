"""/api/v1/learning and /dashboard/learning: settings (audited policy update),
compute, snapshots, metrics, recommendations, dataset, expected signal."""

from datetime import timedelta

from app.pipeline.repository import OpportunityRepository
from tests.conftest import AUTH_HEADERS, TEST_API_KEY
from tests.learning.conftest import T0, history, mixed_specs, spec


def test_api_flow(client, db_session, tenant_id):
    history(db_session, tenant_id, mixed_specs(40))
    db_session.commit()
    settings = client.get("/api/v1/learning/settings").json()
    assert settings["learning_settings"]["ordering_enabled"] is False and settings["priority_learned_prior_weight"] == 0.05 and "admission" in settings["note"]
    assert client.put("/api/v1/learning/settings", json={"window_days": 90}).status_code == 401
    updated = client.put("/api/v1/learning/settings", json={"window_days": 400, "min_samples": 3}, headers=AUTH_HEADERS).json()
    assert updated["learning_settings"]["window_days"] == 400 and updated["learning_settings"]["min_samples"] == 3 and updated["policy_version"] == 2
    events = OpportunityRepository(db_session, tenant_id).list_audit("application_policy")
    assert events[0].action == "updated" and events[0].after["learning_settings"]["window_days"] == 400 and events[0].after["daily_cap"] == events[0].before["daily_cap"]
    assert client.put("/api/v1/learning/settings", json={"min_samples": 0}, headers=AUTH_HEADERS).status_code == 422
    computed = client.get("/api/v1/learning/compute", params={"as_of": (T0 + timedelta(days=60)).isoformat()}).json()
    assert computed["dataset_size"] == 40 and computed["summary"]["ai_calls"] == 0 and computed["baseline"]["n"] == 40 and computed["groups"]
    assert client.get("/api/v1/learning/snapshots/latest").json() is None
    assert client.post("/api/v1/learning/snapshots").status_code == 401
    snap = client.post("/api/v1/learning/snapshots", headers=AUTH_HEADERS).json()
    assert snap["dataset_size"] == 40 and snap["metric_count"] > 0 and snap["learning_version"] == "learning-v1" and snap["window_days"] == 400
    detail = client.get(f"/api/v1/learning/snapshots/{snap['id']}", params={"dimension": "SOURCE", "metric": "response_rate"}).json()
    assert {m["group_key"] for m in detail["metrics"]} == {"GREENHOUSE", "LEVER", "ASHBY"} and all(m["n"] > 0 and m["confidence"] for m in detail["metrics"])
    latest = client.get("/api/v1/learning/snapshots/latest").json()
    assert latest["snapshot"]["id"] == snap["id"] and len(latest["metrics"]) == snap["metric_count"]
    assert isinstance(client.get("/api/v1/learning/recommendations").json(), list)
    assert client.get("/api/v1/learning/snapshots").json()[0]["id"] == snap["id"]
    ds = client.get("/api/v1/learning/dataset", params={"limit": 5}).json()
    assert ds["total"] == 40 and len(ds["rows"]) == 5 and ds["feature_version"] == "features-v1" and "evidence_quality" in ds["rows"][0]
    assert client.get("/api/v1/learning/source-discovery").json()
    assert client.get("/api/v1/learning/expected/nope").status_code == 404
    assert client.get("/api/v1/learning/snapshots/nope").status_code == 404


def test_expected_endpoint_and_dashboard(client, db_session, tenant_id, opportunities):
    co = opportunities.make(company="Warm Co", title="Backend Engineer")
    warm = co.opportunity.company  # the factory suffixes company names per test
    history(db_session, tenant_id, [spec(company=warm, events=[("INTERVIEW_REQUESTED", "STRONG", 1)])] * 12 + [spec(company="Cold Co", events=[("REJECTED", "STRONG", 1)])] * 12)
    db_session.commit()
    off = client.get(f"/api/v1/learning/expected/{co.id}").json()
    assert off["enabled"] is False and off["score"] == 50.0
    client.put("/api/v1/learning/settings", json={"ordering_enabled": True}, headers=AUTH_HEADERS)
    client.post("/api/v1/learning/snapshots", headers=AUTH_HEADERS)
    on = client.get(f"/api/v1/learning/expected/{co.id}").json()
    assert on["enabled"] is True and on["score"] > 50 and on["snapshot_id"] and any(c["dimension"] == "COMPANY" and c["used"] for c in on["components"])
    assert client.get("/dashboard/learning").status_code == 401
    client.get("/dashboard/", params={"key": TEST_API_KEY})
    page = client.get("/dashboard/learning").text
    assert "Outcome learning" in page and "never a filter" in page and "By company" in page and "n=" in page and "Learning settings" in page
    assert client.post("/dashboard/learning/snapshot").status_code == 303
    saved = client.post("/dashboard/learning/settings", data={"ordering_enabled": "on", "window_days": "30", "min_samples": "4", "prior_strength": "8", "minimum_evidence": "STRONG"})
    assert saved.status_code == 303 and "settings-saved" in saved.headers["location"]
    assert client.get("/api/v1/learning/settings").json()["learning_settings"] == {"ordering_enabled": True, "window_days": 30, "min_samples": 4, "prior_strength": 8.0, "minimum_evidence": "STRONG"}
    bad = client.post("/dashboard/learning/settings", data={"window_days": "x"})
    assert bad.status_code == 303 and "error:" in bad.headers["location"]
