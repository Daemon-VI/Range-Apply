"""Phase 8b benchmarks: the AI-disabled path, the cache-hit path, and the
Tier-1 gate / fit-band evaluation over 1,000 (5,000 opt-in) opportunities.

The high-volume path must not become an AI bottleneck: with AI disabled the
gateway answers in microseconds and the expected provider call count is 0.
"""

import os
import time
import tracemalloc

import pytest

from app.ai.gateway import AIGateway, GlobalAIConfig, UsageSink
from app.ai.models import AIStatus
from app.pipeline.gates import evaluate_gates
from app.pipeline.models import ApplicationPolicy, EligibilityDecision, FitBand
from app.pipeline.policy import band_for, evaluate_admission
from tests.ai.conftest import make_gateway, request


def _gate_benchmark(n: int) -> dict:
    policy = ApplicationPolicy(tenant_id="perf", blocked_companies=["Evil Corp"], minimum_fit_score=10)
    decisions = [EligibilityDecision.ELIGIBLE, EligibilityDecision.LIKELY, EligibilityDecision.UNCERTAIN, EligibilityDecision.INELIGIBLE]
    gateway = AIGateway(config=GlobalAIConfig(enabled=False, persist_usage=False), sink=UsageSink(persist=False))
    tracemalloc.start()
    started = time.perf_counter()
    admitted = 0
    by_code: dict[str, int] = {}
    for i in range(n):
        score = (i * 37) % 101
        band = band_for(score, policy.band_thresholds)
        admission = evaluate_admission(policy, decisions[i % 4], band, "Evil Corp" if i % 50 == 0 else f"Company {i % 200}", score)
        by_code[admission.code.value] = by_code.get(admission.code.value, 0) + 1
        admitted += int(admission.admitted)
        # The high-volume path asks the gateway nothing; even if it did, disabled is free.
        if i % 100 == 0:
            assert gateway.run(request(text=str(i)), tenant=None).status is AIStatus.DISABLED
    seconds = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    results = {"opportunities": n, "seconds": round(seconds, 3), "per_second": round(n / max(seconds, 1e-9)), "admitted": admitted, "by_code": by_code, "ai_calls": gateway.stats()["provider_calls"], "python_peak_memory_mb": round(peak / 1024 / 1024, 2)}
    print("\nGATE BENCHMARK:", results)
    assert results["ai_calls"] == 0 and admitted > 0 and by_code.get("INELIGIBLE") == n // 4
    return results


@pytest.mark.perf
def test_gates_1000_opportunities_ai_disabled():
    results = _gate_benchmark(1000)
    assert results["seconds"] < 5


@pytest.mark.perf
@pytest.mark.skipif(os.environ.get("CAREEROS_PERF") != "1", reason="set CAREEROS_PERF=1 for the 5,000-opportunity run")
def test_gates_5000_opportunities_ai_disabled():
    _gate_benchmark(5000)


@pytest.mark.perf
def test_ai_disabled_path_is_free():
    gateway = AIGateway(config=GlobalAIConfig(enabled=False, persist_usage=False), sink=UsageSink(persist=False))
    n = 10_000
    started = time.perf_counter()
    statuses = {gateway.run(request(text=str(i)), tenant=None).status for i in range(n)}
    seconds = time.perf_counter() - started
    results = {"requests": n, "seconds": round(seconds, 3), "per_second": round(n / max(seconds, 1e-9)), "statuses": sorted(s.value for s in statuses), "ai_calls": gateway.stats()["provider_calls"]}
    print("\nAI DISABLED BENCHMARK:", results)
    assert statuses == {AIStatus.DISABLED} and results["ai_calls"] == 0 and seconds < 10


@pytest.mark.perf
def test_ai_cache_hit_path(tmp_path):
    gateway, provider = make_gateway(lambda prompt, json_mode: "answer for " + prompt[-8:], cache_dir=str(tmp_path))
    distinct = 200
    for i in range(distinct):
        gateway.run(request(text=f"input-{i:04d}"), tenant=None)
    assert provider.calls == distinct
    started = time.perf_counter()
    hits = 0
    for round_ in range(5):
        for i in range(distinct):
            r = gateway.run(request(text=f"input-{i:04d}"), tenant=None)
            hits += int(r.cache_hit)
    seconds = time.perf_counter() - started
    results = {"cached_requests": distinct * 5, "cache_hits": hits, "seconds": round(seconds, 3), "per_second": round(distinct * 5 / max(seconds, 1e-9)), "provider_calls": provider.calls, "disk_entries": gateway.cache.count()}
    print("\nAI CACHE-HIT BENCHMARK:", results)
    assert hits == distinct * 5 and provider.calls == distinct and results["disk_entries"] == distinct


@pytest.mark.perf
def test_static_gates_alone_are_microseconds():
    policy = ApplicationPolicy(tenant_id="perf")
    n = 20_000
    started = time.perf_counter()
    passed = sum(1 for i in range(n) if evaluate_gates(policy, EligibilityDecision.ELIGIBLE, FitBand.MEDIUM, f"C{i}", 50).passed)
    seconds = time.perf_counter() - started
    print(f"\nGATE-ONLY BENCHMARK: {n} evaluations in {seconds:.3f}s ({n / seconds:.0f}/s)")
    assert passed == n and seconds < 10
