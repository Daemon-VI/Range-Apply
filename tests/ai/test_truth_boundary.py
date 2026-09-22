"""AI never creates facts: the L2 polisher goes through the gateway, every
rewrite is re-validated against the Evidence Graph, unsupported claims are
rejected, and every failure mode degrades to the deterministic package."""

import pytest

from app.ai.gateway import reset_gateway
from app.ai.models import AIScope, AIStatus, TenantAISettings
from app.ai.providers import ProviderError, ScriptedProvider
from app.career.models import EvidenceStatus
from app.pipeline.models import ApplicationPolicyUpdate
from app.pipeline.repository import PolicyRepository
from app.preparation.ai import GatewayPolisher, StubPolisher, get_polisher
from app.preparation.models import ArtifactKind, PreparationStatus
from app.preparation.service import PreparationService
from tests.ai.conftest import make_gateway
from tests.preparation import conftest as _prep

db_session = _prep.db_session
tenant_id = _prep.tenant_id
other_tenant_id = _prep.other_tenant_id
evidence = _prep.evidence
answered_bank = _prep.answered_bank
jobs = _prep.jobs
matches = _prep.matches
opportunities = _prep.opportunities
service = _prep.service

ON = TenantAISettings(enabled=True)


def _resume(prep):
    return next(a for a in prep.artifacts if a.artifact_type == ArtifactKind.RESUME.value)


def _enable_tenant_ai(db_session, tenant_id, **extra):
    PolicyRepository(db_session, tenant_id).update(ApplicationPolicyUpdate(ai_settings=TenantAISettings(enabled=True, **extra)), "test")
    db_session.commit()


def _polisher(answers, tenant_id, **kwargs) -> tuple[GatewayPolisher, ScriptedProvider]:
    gateway, provider = make_gateway(answers, candidate_providers=("scripted",), **kwargs)
    return GatewayPolisher(gateway, tenant_id, ON), provider


def test_l2_stays_zero_ai_when_ai_is_off(service, opportunities, db_session, tenant_id):
    co = opportunities.make(fit_score=90)  # HIGH -> L2
    prep = service.prepare(co.id)
    assert prep.tailoring_level == "L2" and prep.ai_calls == 0 and not prep.ai_used and prep.ai_provider is None
    assert prep.status == PreparationStatus.READY.value
    assert isinstance(get_polisher(tenant_id, TenantAISettings(enabled=True)), StubPolisher), "AI_ENABLED=false wins over the tenant switch"


def test_faithful_rewrite_is_accepted_with_gateway_metadata(db_session, tenant_id, answered_bank, opportunities):
    co = opportunities.make(fit_score=90)
    polisher, provider = _polisher(lambda prompt, json_mode: prompt.split("SENTENCE: ", 1)[1].split("\n\nSUPPORTING", 1)[0].strip() + " (refined)", tenant_id)
    prep = PreparationService(db_session, tenant_id, polisher=polisher, actor="test").prepare(co.id)
    assert prep.ai_used and prep.ai_calls > 0 and prep.ai_provider.startswith("gateway:scripted") and prep.ai_model == "scripted-1"
    assert any(b["ai_polished"] for b in _resume(prep).blocks) and "(refined)" in _resume(prep).content
    assert provider.calls == prep.ai_calls
    meta = polisher.metadata()
    assert meta["used"] and meta["gateway_version"] and meta["operation"] == "polish_text" and meta["by_status"]["OK"] == provider.calls
    # The audit event carries the gateway metadata, never the prompt or text.
    from app.pipeline.repository import OpportunityRepository

    event = next(e for e in OpportunityRepository(db_session, tenant_id).list_audit("preparation", prep.id) if e.action == "created")
    assert event.after["ai"]["provider"] == "scripted" and event.after["ai"]["rejected_by_truth_gate"] == 0
    assert "SENTENCE" not in str(event.after) and "(refined)" not in str(event.after)


def test_unsupported_claims_are_rejected_and_never_become_evidence(db_session, tenant_id, answered_bank, opportunities, evidence):
    co = opportunities.make(fit_score=90)
    polisher, provider = _polisher(lambda prompt, json_mode: prompt.split("SENTENCE: ", 1)[1].split("\n\nSUPPORTING", 1)[0].strip() + " Led a team of 40 at Google and raised revenue 300% in 2015.", tenant_id)
    prep = PreparationService(db_session, tenant_id, polisher=polisher, actor="test").prepare(co.id)
    assert provider.calls > 0 and not prep.ai_used, "every rewrite failed the truth gate"
    content = _resume(prep).content
    assert "Google" not in content and "300%" not in content and "team of 40" not in content
    assert prep.validation_status == "PASSED" and prep.status == PreparationStatus.READY.value
    assert polisher.metadata()["by_status"]["OK"] == provider.calls, "the gateway delivered; the truth gate refused"
    assert polisher.metadata()["used"], "the gateway itself does not judge truth; the validator does"
    nodes = evidence.list_nodes(include_removed=True)
    assert nodes and not any("Google" in n.claim or "300%" in n.claim or "team of 40" in n.claim for n in nodes), "no AI text entered the Evidence Graph"
    assert all(n.status in (EvidenceStatus.ACTIVE.value, EvidenceStatus.REMOVED.value) for n in nodes)


@pytest.mark.parametrize("failure", [ProviderError("timeout", timeout=True), ProviderError("HTTP 503"), RuntimeError("crash"), "", "   "])
def test_every_provider_failure_degrades_to_the_deterministic_package(db_session, tenant_id, answered_bank, opportunities, failure):
    co = opportunities.make(fit_score=90)
    polisher, provider = _polisher([failure], tenant_id)
    prep = PreparationService(db_session, tenant_id, polisher=polisher, actor="test").prepare(co.id)
    assert prep.status == PreparationStatus.READY.value and not prep.ai_used and prep.ai_calls > 0
    assert provider.calls >= 1
    meta = polisher.metadata()
    assert not meta["used"] and meta["fallback_reasons"]


def test_budget_caps_provider_calls_per_package(db_session, tenant_id, answered_bank, opportunities):
    co = opportunities.make(fit_score=90)
    gateway, provider = make_gateway(["ok"], candidate_providers=("scripted",), max_calls=1)
    polisher = GatewayPolisher(gateway, tenant_id, TenantAISettings(enabled=True, max_calls_per_run=1))
    prep = PreparationService(db_session, tenant_id, polisher=polisher, actor="test").prepare(co.id)
    assert prep.ai_calls > 1 and provider.calls == 1
    assert polisher.metadata()["by_status"].get("BUDGET_EXHAUSTED", 0) == prep.ai_calls - 1
    assert prep.status == PreparationStatus.READY.value


def test_cache_hit_path_serves_a_second_package_without_a_call(db_session, tenant_id, answered_bank, opportunities):
    co = opportunities.make(fit_score=90)
    gateway, provider = make_gateway(lambda prompt, json_mode: prompt.split("SENTENCE: ", 1)[1].split("\n\nSUPPORTING", 1)[0].strip() + " (cached)", candidate_providers=("scripted",))
    polisher = GatewayPolisher(gateway, tenant_id, ON)
    svc = PreparationService(db_session, tenant_id, polisher=polisher, actor="test")
    first = svc.prepare(co.id)
    calls_after_first = provider.calls
    second = svc.prepare(co.id, force=True)
    assert first.ai_used and second.ai_used and provider.calls == calls_after_first
    assert polisher.cache_hits >= calls_after_first
    assert _resume(second).content == _resume(first).content


def test_polisher_is_candidate_scoped_and_tenant_private(db_session, tenant_id, other_tenant_id):
    gateway, provider = make_gateway(["one", "two"], candidate_providers=("scripted",))
    a = GatewayPolisher(gateway, tenant_id, ON).polish("Built X.", "evidence", "resume bullet")
    b = GatewayPolisher(gateway, other_tenant_id, ON).polish("Built X.", "evidence", "resume bullet")
    assert a == "one" and b == "two" and provider.calls == 2
    recent = list(gateway.sink.recent)
    assert {r["scope"] for r in recent} == {AIScope.CANDIDATE.value}
    assert GatewayPolisher(gateway, tenant_id, ON).polish("Built X.", "evidence", "resume bullet") == "one"
    hosted = ScriptedProvider(["leak"], name="hostedllm")
    hosted.local = False
    refusing, _ = make_gateway(scripted=hosted, provider="hostedllm", candidate_providers=("ollama",))
    refusing._provider = hosted
    polisher = GatewayPolisher(refusing, tenant_id, ON)
    assert polisher.polish("Built X.", "evidence", "resume bullet") is None and hosted.calls == 0
    assert polisher.statuses[AIStatus.REFUSED.value] == 1


def test_tenant_policy_switch_selects_the_polisher(db_session, tenant_id, monkeypatch):
    gateway, _ = make_gateway(["x"], candidate_providers=("scripted",))
    reset_gateway(gateway)
    assert isinstance(get_polisher(tenant_id, TenantAISettings()), StubPolisher)
    assert isinstance(get_polisher(tenant_id, TenantAISettings(enabled=True)), GatewayPolisher)
    _enable_tenant_ai(db_session, tenant_id, max_calls_per_run=2)
    svc = PreparationService(db_session, tenant_id, actor="test")
    ctx = svc.context()
    assert isinstance(ctx.polisher, GatewayPolisher) and ctx.polisher.budget.max_calls == 2
    reset_gateway(None)


def test_fingerprint_is_neutral_to_ai_availability(db_session, tenant_id, answered_bank, opportunities):
    """A package's inputs (and therefore staleness) do not change because the
    polisher happened to answer or not; the provider name is part of the inputs."""
    co = opportunities.make(fit_score=90)
    gateway, _ = make_gateway([ProviderError("down")], candidate_providers=("scripted",))
    failing = PreparationService(db_session, tenant_id, polisher=GatewayPolisher(gateway, tenant_id, ON), actor="test").prepare(co.id)
    gateway2, _ = make_gateway(["fine"], candidate_providers=("scripted",))
    working = PreparationService(db_session, tenant_id, polisher=GatewayPolisher(gateway2, tenant_id, ON), actor="test").prepare(co.id, force=True)
    assert failing.input_fingerprint == working.input_fingerprint and failing.inputs["ai"] == working.inputs["ai"]
