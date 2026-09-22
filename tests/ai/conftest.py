"""Gateway fixtures: a gateway with a scripted provider, a memory cache and a
memory-only usage sink, isolated from process settings."""

import pytest
from pydantic import BaseModel

from app.ai.cache import MemoryCache
from app.ai.gateway import AIGateway, GlobalAIConfig, UsageSink, reset_gateway
from app.ai.models import AIOperation, AIRequest, AIScope, TenantAISettings
from app.ai.providers import ScriptedProvider


class Answer(BaseModel):
    label: str
    score: int


def make_gateway(answers=None, *, enabled=True, provider="scripted", cache=True, max_calls=50, timeout=5.0, candidate_providers=("ollama",), scripted=None, persist=False, cache_dir=None) -> tuple[AIGateway, ScriptedProvider]:
    scripted = scripted or ScriptedProvider(answers)
    config = GlobalAIConfig(enabled=enabled, provider=provider, model=None, timeout_seconds=timeout, max_calls_per_run=max_calls, max_output_tokens=512, cache_enabled=cache, cache_dir=cache_dir, candidate_data_providers=list(candidate_providers), persist_usage=persist)

    def factory(name, model):
        if name == "scripted":
            return scripted
        from app.ai.providers import build_provider

        return build_provider(name, model)

    gateway = AIGateway(config=config, cache=None if cache_dir else MemoryCache(), sink=UsageSink(persist=persist), provider_factory=factory)
    return gateway, scripted


def request(op=AIOperation.CLASSIFY, tenant="t1", scope=AIScope.JOB, text="hello", schema=None, **kwargs) -> AIRequest:
    return AIRequest(operation=op, tenant_id=tenant, scope=scope, prompt=f"Classify: {text}", input={"text": text}, schema_model=schema, **kwargs)


TENANT_ON = TenantAISettings(enabled=True)


@pytest.fixture(autouse=True)
def _isolated_gateway():
    reset_gateway(None)
    yield
    reset_gateway(None)


@pytest.fixture
def gateway():
    gw, provider = make_gateway(["polished text"])
    return gw


__all__ = ["Answer", "make_gateway", "request", "TENANT_ON"]
