"""Phase 11 benchmarks: dataset construction, aggregation, snapshot
generation and recommendations over 1,000 / 5,000 / 10,000 executed
applications. AI calls: zero. Memory via tracemalloc on the aggregation."""

import os
import time
import tracemalloc

import pytest

from app.ai.gateway import AIGateway, GlobalAIConfig, UsageSink, reset_gateway
from app.learning.database.models import (
    LearningMetricRow,
    LearningRecommendationRow,
    LearningSnapshotRow,
)
from app.learning.engine import LearningEngine
from app.learning.models import TenantLearningSettings
from tests.learning.conftest import history, mixed_specs


def _bench(db_session, tenant_id, n: int) -> dict:
    gateway = AIGateway(config=GlobalAIConfig(enabled=False, persist_usage=False), sink=UsageSink(persist=False))
    reset_gateway(gateway)
    started = time.perf_counter()
    # 3 minutes apart so even 10,000 applications sit in the past (the dataset is time-aware)
    history(db_session, tenant_id, mixed_specs(n, companies=max(10, n // 20)), spacing_hours=0.05)
    seed_seconds = time.perf_counter() - started
    engine = LearningEngine(db_session, tenant_id, TenantLearningSettings(), actor="perf")
    started = time.perf_counter()
    rows = engine.dataset()
    dataset_seconds = time.perf_counter() - started
    tracemalloc.start()
    started = time.perf_counter()
    result = engine.aggregate(rows)
    aggregate_seconds = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    started = time.perf_counter()
    recs = engine.recommendations(result)
    recommend_seconds = time.perf_counter() - started
    before = (db_session.query(LearningSnapshotRow).count(), db_session.query(LearningMetricRow).count(), db_session.query(LearningRecommendationRow).count())
    started = time.perf_counter()
    snap = engine.snapshot(actor="perf")
    db_session.commit()
    snapshot_seconds = time.perf_counter() - started
    after = (db_session.query(LearningSnapshotRow).count(), db_session.query(LearningMetricRow).count(), db_session.query(LearningRecommendationRow).count())
    results = {
        "applications": n,
        "seed_seconds": round(seed_seconds, 2),
        "dataset_seconds": round(dataset_seconds, 3),
        "dataset_rows_per_second": round(n / max(dataset_seconds, 1e-9)),
        "aggregate_seconds": round(aggregate_seconds, 3),
        "groups": len(result.groups),
        "recommend_seconds": round(recommend_seconds, 3),
        "recommendations": len(recs),
        "snapshot_seconds": round(snapshot_seconds, 3),
        "db_rows_written": {"snapshots": after[0] - before[0], "metrics": after[1] - before[1], "recommendations": after[2] - before[2]},
        "aggregate_peak_memory_mb": round(peak / 1024 / 1024, 2),
        "ai_calls": gateway.stats()["provider_calls"],
        "ai_requests": gateway.stats()["requests"],
    }
    print(f"\nLEARNING BENCHMARK ({n}):", results)
    reset_gateway(None)
    assert results["ai_calls"] == 0 and results["ai_requests"] == 0 and len(rows) == n and snap.dataset_size == n
    return results


@pytest.mark.perf
def test_learning_1000(db_session, tenant_id):
    r = _bench(db_session, tenant_id, 1000)
    assert r["dataset_seconds"] < 30 and r["aggregate_seconds"] < 30 and r["snapshot_seconds"] < 60


@pytest.mark.perf
def test_learning_5000(db_session, tenant_id):
    r = _bench(db_session, tenant_id, 5000)
    assert r["dataset_seconds"] < 120 and r["aggregate_seconds"] < 60


@pytest.mark.perf
@pytest.mark.skipif(os.environ.get("CAREEROS_PERF") != "1", reason="set CAREEROS_PERF=1 for the 10,000-application run")
def test_learning_10000(db_session, tenant_id):
    _bench(db_session, tenant_id, 10000)
