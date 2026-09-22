"""The AI Gateway: disabled / provider selection / cache / timeout / error /
budget / malformed / fallback / metadata / tenant isolation."""

import json
import time

import pytest

from app.ai.budget import CallBudget
from app.ai.cache import AICache, cache_key
from app.ai.gateway import AIGateway, GlobalAIConfig, get_gateway, reset_gateway
from app.ai.models import GATEWAY_VERSION, AIScope, AIStatus, TenantAISettings
from app.ai.providers import ProviderError, ScriptedProvider, StubProvider, build_provider
from app.config import settings
from tests.ai.conftest import Answer, make_gateway, request

# ------------------------------------------------------------- disabled


def test_default_settings_disable_ai_completely(monkeypatch):
    monkeypatch.setattr(settings, "ai_enabled", False)
    monkeypatch.setattr(settings, "llm_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "AIzaFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE")
    reset_gateway(None)
    gateway = get_gateway()
    assert gateway.config.enabled is False and gateway.config.provider == "gemini", "legacy LLM_PROVIDER selects the provider but never turns AI on"
    response = gateway.run(request(), tenant=None)
    assert response.status is AIStatus.DISABLED and response.output is None and "AI_ENABLED" in response.fallback_reason
    assert gateway.stats()["provider_calls"] == 0


def test_tenant_switch_off_by_default_even_when_global_is_on():
    gateway, provider = make_gateway(["x"])
    off = gateway.run(request(scope=AIScope.CANDIDATE), tenant=TenantAISettings())
    assert off.status is AIStatus.DISABLED and "tenant" in off.fallback_reason and provider.calls == 0
    on = gateway.run(request(scope=AIScope.JOB), tenant=None)
    assert on.status is AIStatus.OK and on.output == "x"


def test_tenant_can_narrow_but_never_widen():
    gateway, _ = make_gateway(["x"], max_calls=10, timeout=5.0)
    eff = gateway.effective(TenantAISettings(enabled=True, max_calls_per_run=3, timeout_seconds=1.0, cache_enabled=False, provider="gemini"))
    assert eff.max_calls_per_run == 3 and eff.timeout_seconds == 1.0 and eff.cache_enabled is False
    assert eff.provider == "scripted", "a tenant cannot pick a provider the operator did not configure"
    wide = gateway.effective(TenantAISettings(enabled=True, max_calls_per_run=1000, timeout_seconds=100.0))
    assert wide.max_calls_per_run == 10 and wide.timeout_seconds == 5.0
    disabled_gateway, _ = make_gateway(["x"], enabled=False)
    assert disabled_gateway.effective(TenantAISettings(enabled=True)).enabled is False


# ---------------------------------------------------------- providers


def test_provider_missing_unsupported_and_stub_are_controlled_results():
    gateway, _ = make_gateway(["x"], provider="stub")
    response = gateway.run(request(), tenant=None)
    assert response.status is AIStatus.UNAVAILABLE and "not configured" in response.fallback_reason
    gateway, _ = make_gateway(["x"], provider="nonexistent-llm")
    response = gateway.run(request(), tenant=None)
    assert response.status is AIStatus.UNSUPPORTED
    gateway, _ = make_gateway(["x"])
    response = gateway.run(request(provider="gemini"), tenant=None)
    assert response.status is AIStatus.UNAVAILABLE, "gemini without a key is unavailable, not an exception"
    assert isinstance(build_provider("stub", None), StubProvider) and build_provider("bogus", None) is None
    assert build_provider("gemini", None, gemini_api_key=None).configured is False
    assert build_provider("ollama", "llama3.2").local is True


def test_timeout_error_and_crash_fall_back():
    gateway, _ = make_gateway([ProviderError("slow", timeout=True)])
    assert gateway.run(request(), tenant=None).status is AIStatus.TIMEOUT
    gateway, _ = make_gateway([ProviderError("HTTP 500")])
    assert gateway.run(request(), tenant=None).status is AIStatus.ERROR
    gateway, _ = make_gateway([RuntimeError("boom")])
    response = gateway.run(request(), tenant=None)
    assert response.status is AIStatus.ERROR and "crashed" in response.fallback_reason
    stats = gateway.stats()
    assert stats["by_status"]["ERROR"] == 1 and stats["provider_calls"] == 0


def test_request_timeout_is_capped_by_config_and_passed_to_provider():
    seen = {}

    def answer(prompt, json_mode):
        return "ok"

    provider = ScriptedProvider(answer)
    original = provider.complete

    def complete(prompt, *, json_mode, timeout_seconds, max_output_tokens):
        seen["timeout"] = timeout_seconds
        seen["tokens"] = max_output_tokens
        return original(prompt, json_mode=json_mode, timeout_seconds=timeout_seconds, max_output_tokens=max_output_tokens)

    provider.complete = complete
    gateway, _ = make_gateway(scripted=provider, timeout=5.0)
    gateway.run(request(timeout_seconds=60.0, max_output_tokens=9999), tenant=None)
    assert seen["timeout"] == 5.0 and seen["tokens"] == 512


# ------------------------------------------------------------ outputs


def test_structured_output_validates_against_schema_and_malformed_falls_back():
    gateway, provider = make_gateway([json.dumps({"label": "yes", "score": 3})])
    response = gateway.run(request(schema=Answer), tenant=None)
    assert response.status is AIStatus.OK and response.output == {"label": "yes", "score": 3}
    for bad in ("not json", json.dumps({"label": "yes"}), json.dumps(["list"]), json.dumps({"label": "yes", "score": "high"}), ""):
        gateway, _ = make_gateway([bad])
        response = gateway.run(request(schema=Answer), tenant=None)
        assert response.status is AIStatus.MALFORMED, bad
    gateway, _ = make_gateway(["```json\n{\"label\": \"fenced\", \"score\": 1}\n```"])
    assert gateway.run(request(schema=Answer), tenant=None).output["label"] == "fenced"
    gateway, _ = make_gateway(["   "])
    assert gateway.run(request(), tenant=None).status is AIStatus.MALFORMED, "empty text is malformed"


# -------------------------------------------------------------- cache


def test_cache_hit_miss_and_key_includes_every_semantic_input():
    gateway, provider = make_gateway(["first", "second"])
    a = gateway.run(request(text="same"), tenant=None)
    b = gateway.run(request(text="same"), tenant=None)
    assert a.output == b.output == "first" and b.cache_hit and not a.cache_hit and provider.calls == 1
    c = gateway.run(request(text="different"), tenant=None)
    assert c.output == "second" and provider.calls == 2
    base = dict(operation="classify", prompt_version="v1", schema_version="v1", provider="p", model="m", scope=AIScope.JOB, tenant_id="t", input_data={"text": "x"})
    key = cache_key(**base)
    for change in ({"operation": "summarize"}, {"prompt_version": "v2"}, {"schema_version": "v2"}, {"provider": "q"}, {"model": "m2"}, {"input_data": {"text": "y"}}):
        assert cache_key(**{**base, **change}) != key, change
    assert cache_key(**{**base, "tenant_id": "other"}) == key, "job-side entries are shared"
    assert cache_key(**{**base, "scope": AIScope.CANDIDATE, "tenant_id": "other"}) != cache_key(**{**base, "scope": AIScope.CANDIDATE}), "candidate-side entries are tenant-private"


def test_cache_can_be_disabled_globally_per_tenant_or_per_request():
    gateway, provider = make_gateway(["a", "b", "c"], cache=False)
    gateway.run(request(), tenant=None)
    gateway.run(request(), tenant=None)
    assert provider.calls == 2
    gateway, provider = make_gateway(["a", "b", "c"])
    gateway.run(request(scope=AIScope.CANDIDATE), tenant=TenantAISettings(enabled=True, cache_enabled=False))
    gateway.run(request(scope=AIScope.CANDIDATE), tenant=TenantAISettings(enabled=True, cache_enabled=False))
    assert provider.calls == 2
    gateway.run(request(cache=False), tenant=None)
    gateway.run(request(cache=False), tenant=None)
    assert provider.calls == 4


def test_stale_or_corrupt_disk_cache_entries_are_misses(tmp_path):
    gateway, provider = make_gateway([json.dumps({"label": "v", "score": 1}), json.dumps({"label": "w", "score": 2})], cache_dir=str(tmp_path))
    first = gateway.run(request(schema=Answer), tenant=None)
    assert first.status is AIStatus.OK and isinstance(gateway.cache, AICache) and gateway.cache.count() == 1
    path = next(tmp_path.rglob("*.json"))
    # A schema change makes the old entry stale (validation fails) -> refetch.
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["output"] = {"label": "v"}
    path.write_text(json.dumps(entry), encoding="utf-8")
    second = gateway.run(request(schema=Answer), tenant=None)
    assert second.output == {"label": "w", "score": 2} and provider.calls == 2
    path.write_text("{corrupt", encoding="utf-8")
    third = gateway.run(request(schema=Answer), tenant=None)
    assert third.status is AIStatus.OK and provider.calls == 3
    assert gateway.cache.clear() >= 1 and gateway.cache.count() == 0


def test_cache_entries_hold_no_prompt_and_version_bump_invalidates(tmp_path, monkeypatch):
    gateway, _ = make_gateway(["polished"], cache_dir=str(tmp_path))
    gateway.run(request(text="SECRET-EVIDENCE-LINE"), tenant=None)
    raw = next(tmp_path.rglob("*.json")).read_text(encoding="utf-8")
    assert "Classify:" not in raw and "SECRET-EVIDENCE-LINE" not in raw and GATEWAY_VERSION in raw
    key_v1 = cache_key(operation="classify", prompt_version="v1", schema_version="v1", provider="scripted", model="scripted-1", scope=AIScope.JOB, tenant_id="t1", input_data={"text": "x"})
    monkeypatch.setattr("app.ai.cache.GATEWAY_VERSION", "ai-gateway-v99")
    key_v2 = cache_key(operation="classify", prompt_version="v1", schema_version="v1", provider="scripted", model="scripted-1", scope=AIScope.JOB, tenant_id="t1", input_data={"text": "x"})
    assert key_v1 != key_v2


# --------------------------------------------------------- tenant scope


def test_candidate_data_never_crosses_tenants_and_hosted_providers_are_refused():
    gateway, provider = make_gateway(["for t1", "for t2"])
    on = TenantAISettings(enabled=True)
    a = gateway.run(request(scope=AIScope.CANDIDATE, tenant="t1", text="my evidence"), tenant=on)
    b = gateway.run(request(scope=AIScope.CANDIDATE, tenant="t2", text="my evidence"), tenant=on)
    assert a.output == "for t1" and b.output == "for t2" and not b.cache_hit and provider.calls == 2
    again = gateway.run(request(scope=AIScope.CANDIDATE, tenant="t1", text="my evidence"), tenant=on)
    assert again.cache_hit and again.output == "for t1"
    # A hosted provider not listed for candidate data is refused before any call.
    hosted = ScriptedProvider(["leak"], name="hostedllm")
    hosted.local = False
    gateway, _ = make_gateway(scripted=hosted, provider="hostedllm", candidate_providers=("ollama",))
    gateway._provider = hosted
    refused = gateway.run(request(scope=AIScope.CANDIDATE, tenant="t1"), tenant=on)
    assert refused.status is AIStatus.REFUSED and hosted.calls == 0
    allowed_gateway, _ = make_gateway(scripted=hosted, provider="hostedllm", candidate_providers=("ollama", "hostedllm"))
    allowed_gateway._provider = hosted
    assert allowed_gateway.run(request(scope=AIScope.CANDIDATE, tenant="t1"), tenant=on).status is AIStatus.OK


def test_prompts_with_credentials_are_refused_before_any_call(monkeypatch):
    gateway, provider = make_gateway(["x"])
    for prompt in ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123", "api_key=ABCDEFGHIJKLMNOP", "Cookie: session=abc", "password: hunter2", "AIzaSyD-1234567890abcdefghijklmnopqrstuvw"):
        r = gateway.run(request().model_copy(update={"prompt": prompt}), tenant=None)
        assert r.status is AIStatus.REFUSED, prompt
    monkeypatch.setattr(settings, "api_key", "my-real-api-key-value")
    r = gateway.run(request().model_copy(update={"prompt": "the key is my-real-api-key-value"}), tenant=None)
    assert r.status is AIStatus.REFUSED and provider.calls == 0


# --------------------------------------------------------------- budget


def test_budget_exhaustion_is_controlled_and_cache_hits_are_free():
    gateway, provider = make_gateway(["a", "b", "c", "d"])
    budget = CallBudget(2, "run")
    assert gateway.run(request(text="1"), tenant=None, budget=budget).status is AIStatus.OK
    assert gateway.run(request(text="1"), tenant=None, budget=budget).cache_hit is True
    assert gateway.run(request(text="2"), tenant=None, budget=budget).status is AIStatus.OK
    third = gateway.run(request(text="3"), tenant=None, budget=budget)
    assert third.status is AIStatus.BUDGET_EXHAUSTED and provider.calls == 2 and budget.refused == 1
    assert gateway.run(request(text="1"), tenant=None, budget=budget).cache_hit, "cached answers keep flowing"
    zero = CallBudget(0, "paused")
    assert gateway.run(request(text="9"), tenant=None, budget=zero).status is AIStatus.BUDGET_EXHAUSTED
    assert CallBudget(None).can_call() and CallBudget(None).remaining is None
    b = gateway.budget(TenantAISettings(enabled=True, max_calls_per_run=1))
    assert b.max_calls == 1


# ------------------------------------------------------------ metadata


def test_every_response_carries_accounting_metadata_and_sink_counts():
    gateway, _ = make_gateway(["text"])
    ok = gateway.run(request(reference="prep-1"), tenant=None)
    meta = ok.metadata()
    assert meta["status"] == "OK" and meta["provider"] == "scripted" and meta["model"] == "scripted-1" and meta["gateway_version"] == GATEWAY_VERSION and meta["prompt_version"] == "v1"
    assert "prompt" not in meta and "output" not in meta
    assert ok.usage.input_tokens and ok.usage.output_tokens and ok.latency_ms >= 0
    hit = gateway.run(request(reference="prep-1"), tenant=None)
    stats = gateway.stats()
    assert stats["requests"] == 2 and stats["provider_calls"] == 1 and stats["cache_hits"] == 1 and stats["by_operation"]["classify"] == 2
    assert stats["cache"]["hits"] == 1 and stats["cache"]["misses"] == 1 and stats["cache"]["writes"] == 1
    recent = list(gateway.sink.recent)
    assert recent[0]["reference"] == "prep-1" and recent[0]["cache_hit"] is True and hit.cache_key == ok.cache_key


def test_usage_rows_are_persisted_per_tenant_without_prompts(db_session_ai):
    from app.ai.database.models import AIUsageRow

    gateway, _ = make_gateway(["text"], persist=True)
    tenant = f"ai-usage-{int(time.time() * 1000)}"
    gateway.run(request(tenant=tenant, reference="ref-x", text="private words"), tenant=None)
    gateway.run(request(tenant=tenant, text="private words"), tenant=None)
    rows = db_session_ai.query(AIUsageRow).filter(AIUsageRow.tenant_id == tenant).order_by(AIUsageRow.created_at).all()
    assert [r.status for r in rows] == ["OK", "OK"] and [r.cache_hit for r in rows] == [False, True]
    assert rows[0].reference == "ref-x" and rows[0].gateway_version == GATEWAY_VERSION
    for row in rows:
        for column in ("operation", "provider", "model", "status", "fallback_reason", "reference"):
            assert "private words" not in str(getattr(row, column) or "")
    for row in rows:
        db_session_ai.delete(row)
    db_session_ai.commit()


@pytest.fixture
def db_session_ai():
    from app.database import get_session_factory

    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def test_global_config_from_settings_and_singleton(monkeypatch):
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "ai_provider", "ollama")
    monkeypatch.setattr(settings, "ai_max_calls_per_run", 7)
    monkeypatch.setattr(settings, "ai_candidate_data_providers", "ollama, gemini")
    cfg = GlobalAIConfig.from_settings()
    assert cfg.enabled and cfg.provider == "ollama" and cfg.max_calls_per_run == 7 and cfg.candidate_data_providers == ["ollama", "gemini"]
    reset_gateway(None)
    first = get_gateway()
    assert get_gateway() is first and isinstance(first, AIGateway)
    custom = AIGateway(config=cfg)
    reset_gateway(custom)
    assert get_gateway() is custom
