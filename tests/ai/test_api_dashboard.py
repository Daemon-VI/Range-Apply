"""/api/v1/ai and the policy page: inspectable, auditable, no secrets, per tenant."""

import pytest
from fastapi.testclient import TestClient

from app.ai.gateway import reset_gateway
from app.api.deps import get_tenant_id
from app.config import settings
from app.main import app
from app.pipeline.repository import OpportunityRepository, PolicyRepository
from tests.ai.conftest import make_gateway
from tests.conftest import AUTH_HEADERS
from tests.pipeline import conftest as _pipe

db_session = _pipe.db_session
tenant_id = _pipe.tenant_id
other_tenant_id = _pipe.other_tenant_id


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


def test_config_is_inspectable_and_carries_no_secrets(client, tenant_id, monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "AIzaSECRETSECRETSECRETSECRETSECRETSECRET")
    reset_gateway(None)
    config = client.get("/api/v1/ai/config").json()
    assert config["tenant_id"] == tenant_id and config["effective"]["enabled"] is False and config["effective"]["global_enabled"] is False
    assert config["tenant_settings"]["enabled"] is False and "gemini" in config["known_providers"]
    assert "SECRET" not in client.get("/api/v1/ai/config").text and "cache_dir" not in config["global_config"]


def test_tenant_settings_are_versioned_audited_and_isolated(client, tenant_id, other_tenant_id, db_session):
    assert client.put("/api/v1/ai/config", json={"enabled": True}).status_code == 401
    updated = client.put("/api/v1/ai/config", json={"enabled": True, "max_calls_per_run": 3, "provider": "ollama"}, headers=AUTH_HEADERS).json()
    assert updated["tenant_settings"] == {"enabled": True, "provider": "ollama", "model": None, "max_calls_per_run": 3, "max_output_tokens": None, "timeout_seconds": None, "cache_enabled": None}
    assert updated["policy_version"] == 2 and updated["effective"]["enabled"] is False, "AI_ENABLED=false still wins"
    events = OpportunityRepository(db_session, tenant_id).list_audit("application_policy")
    assert events[0].action == "updated" and events[0].after["ai_settings"]["enabled"] is True and events[0].before["ai_settings"] == {}
    assert PolicyRepository(db_session, other_tenant_id).get().ai_settings.enabled is False
    bad = client.put("/api/v1/ai/config", json={"provider": "skynet"}, headers=AUTH_HEADERS)
    assert bad.status_code == 422


def test_status_usage_and_cache_clear(client, tenant_id, other_tenant_id):
    from tests.ai.conftest import request

    gateway, _ = make_gateway(["x", "y"], persist=True)
    reset_gateway(gateway)
    try:
        gateway.run(request(tenant=tenant_id, text="mine"), tenant=None)
        gateway.run(request(tenant=tenant_id, text="mine"), tenant=None)
        gateway.run(request(tenant=other_tenant_id, text="theirs"), tenant=None)
        status = client.get("/api/v1/ai/status").json()
        assert status["gateway"]["provider_calls"] == 2 and status["gateway"]["cache_hits"] == 1
        assert status["tenant_usage"]["by_status"] == {"OK": 2} and all(r["tenant_id"] == tenant_id for r in status["recent"])
        usage = client.get("/api/v1/ai/usage", params={"operation": "classify"}).json()
        assert len(usage) == 2 and {u["cache_hit"] for u in usage} == {True, False}
        assert all("mine" not in str(u) for u in usage)
        assert client.post("/api/v1/ai/cache/clear").status_code == 401
        assert client.post("/api/v1/ai/cache/clear", headers=AUTH_HEADERS).json()["removed"] == 2
    finally:
        from app.ai.database.models import AIUsageRow
        from app.database import get_session_factory

        session = get_session_factory()()
        session.query(AIUsageRow).filter(AIUsageRow.tenant_id.in_([tenant_id, other_tenant_id])).delete(synchronize_session=False)
        session.commit()
        session.close()
        reset_gateway(None)


def test_policy_page_shows_bands_gates_and_ai_and_saves_ai_settings(client, tenant_id, db_session):
    client.get("/dashboard/", params={"key": AUTH_HEADERS["X-API-Key"]})  # dashboard sign-in cookie
    page = client.get("/dashboard/policy")
    assert page.status_code == 200
    html = page.text
    assert "tier1-gates-v3" in html and "COMPANY_BLOCKLIST" in html and "config v1" in html and "AI (optional)" in html
    saved = client.post("/dashboard/policy", data={"enabled_bands": ["HIGH", "MEDIUM", "LOW"], "high_threshold": 70, "medium_threshold": 45, "daily_cap": 50, "weekly_cap": 300, "cooldown_days": 90, "minimum_eligibility": "UNCERTAIN", "blocked_companies": "", "lane_high": "REVIEW", "lane_medium": "REVIEW", "lane_low": "REVIEW", "tailoring_high": "L2", "tailoring_medium": "L1", "tailoring_low": "L0", "minimum_fit_score": "", "ai_enabled": "on", "ai_max_calls_per_run": "4", "ai_cache_enabled": "on"}, follow_redirects=False)
    assert saved.status_code == 303 and "policy-saved" in saved.headers["location"]
    policy = PolicyRepository(db_session, tenant_id).get()
    assert policy.ai_settings.enabled is True and policy.ai_settings.max_calls_per_run == 4 and policy.ai_settings.cache_enabled is True and policy.version == 2
