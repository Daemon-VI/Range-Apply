"""AI job-posting extraction needs its own opt-in, even when AI is on for answers."""

from types import SimpleNamespace

import app.ai.gateway as gateway_module
from app.config import settings
from app.jobs.extraction.llm_provider import GatewayLLMProvider, StubProvider, get_llm_provider


def _enabled_gateway():
    return SimpleNamespace(effective=lambda tenant=None: SimpleNamespace(enabled=True, provider="gemini", model="m"), budget=lambda *a, **k: None)


def test_discovery_uses_the_stub_unless_job_extraction_is_opted_in(monkeypatch):
    monkeypatch.setattr(gateway_module, "get_gateway", _enabled_gateway)
    monkeypatch.setattr(settings, "ai_job_extraction_enabled", False)
    assert isinstance(get_llm_provider(), StubProvider), "AI on for answers must not spend the quota on every posting"


def test_opting_in_uses_the_gateway(monkeypatch):
    monkeypatch.setattr(gateway_module, "get_gateway", _enabled_gateway)
    monkeypatch.setattr(settings, "ai_job_extraction_enabled", True)
    assert isinstance(get_llm_provider(), GatewayLLMProvider)


def test_the_default_is_off():
    assert type(settings).model_fields["ai_job_extraction_enabled"].default is False
