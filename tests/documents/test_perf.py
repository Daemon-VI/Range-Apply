"""Document benchmark: 100 resumes + 100 cover letters from deterministic preparations.

Measures generation time, artifacts per minute, DB writes, file sizes and
Python peak memory; asserts zero AI calls and that a second pass over the
same preparations reuses every artifact.
"""

import os
import time
import tracemalloc

import pytest
from sqlalchemy import event

from app.database import get_engine
from app.documents.models import DocumentFormat
from app.documents.service import DocumentService
from app.documents.storage import ArtifactStore
from app.pipeline.models import ApplicationPolicyUpdate
from app.pipeline.repository import PolicyRepository
from app.preparation.service import PreparationService
from tests.discovery.conftest import WriteCounter


def _benchmark(db_session, tenant_id, opportunities, docs_root, n: int) -> dict:
    PolicyRepository(db_session, tenant_id).update(ApplicationPolicyUpdate(cover_letter_by_band={"HIGH": "LIGHT", "MEDIUM": "LIGHT", "LOW": "LIGHT"}), "perf")
    db_session.commit()
    cos = [opportunities.make(title=f"Engineer {i % 7}", fit_score=60 + i % 40, company=f"DocCo {i}").id for i in range(n)]
    report = PreparationService(db_session, tenant_id, actor="perf").prepare_many(cos, commit_every=50)
    assert report.errors == [] and report.by_status.get("READY") == n and report.ai_calls == 0
    service = PreparationService(db_session, tenant_id)
    preparations = [service.latest(co_id) for co_id in cos]
    documents = DocumentService(db_session, tenant_id, actor="perf", store=ArtifactStore(str(docs_root)))
    counter = WriteCounter()
    engine = get_engine()
    event.listen(engine, "after_cursor_execute", counter)
    tracemalloc.start()
    started = time.perf_counter()
    sizes = {"RESUME": [], "COVER_LETTER": []}
    for prep in preparations:
        for artifact_type in ("RESUME", "COVER_LETTER"):
            row, created = documents.get_or_render(prep.id, artifact_type, DocumentFormat.PDF)
            assert created and row.validation_status == "PASSED"
            sizes[artifact_type].append(row.byte_size)
    db_session.commit()
    seconds = time.perf_counter() - started
    writes_first = counter.writes
    # Second pass: everything is reused, nothing is regenerated.
    started2 = time.perf_counter()
    for prep in preparations:
        for artifact_type in ("RESUME", "COVER_LETTER"):
            _, created = documents.get_or_render(prep.id, artifact_type, DocumentFormat.PDF)
            assert not created
    seconds2 = time.perf_counter() - started2
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    event.remove(engine, "after_cursor_execute", counter)
    files = list(docs_root.rglob("*.pdf"))
    results = {
        "preparations": n,
        "documents": 2 * n,
        "seconds": round(seconds, 2),
        "documents_per_minute": round(2 * n / max(seconds, 1e-6) * 60),
        "reuse_pass_seconds": round(seconds2, 2),
        "db_writes_render_pass": writes_first,
        "db_writes_reuse_pass": counter.writes - writes_first,
        "files_on_disk": len(files),
        "resume_bytes_avg": round(sum(sizes["RESUME"]) / n),
        "cover_letter_bytes_avg": round(sum(sizes["COVER_LETTER"]) / n),
        "python_peak_memory_mb": round(peak / 1024 / 1024, 1),
        "rendered": documents.stats["rendered"],
        "reused": documents.stats["reused"],
        "ai_calls": 0,
    }
    print("\nDOCUMENT BENCHMARK:", results)
    assert results["files_on_disk"] == 2 * n and documents.stats == {"rendered": 2 * n, "reused": 2 * n, "invalidated": 0}
    return results


@pytest.mark.perf
def test_render_100_resumes_and_cover_letters(db_session, tenant_id, opportunities, documents, docs_root):
    results = _benchmark(db_session, tenant_id, opportunities, docs_root, 100)
    assert results["ai_calls"] == 0 and results["db_writes_reuse_pass"] == 0


@pytest.mark.perf
@pytest.mark.skipif(os.environ.get("CAREEROS_PERF") != "1", reason="set CAREEROS_PERF=1 for the 500-preparation run")
def test_render_500_resumes_and_cover_letters(db_session, tenant_id, opportunities, documents, docs_root):
    results = _benchmark(db_session, tenant_id, opportunities, docs_root, 500)
    assert results["ai_calls"] == 0
