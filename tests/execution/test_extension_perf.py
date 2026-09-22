"""Extension benchmark: the page scripts + the service-worker flow over local
fixture forms (50 always, 200 opt-in with CAREEROS_PERF=1).

Measures wall time per form, form discovery time, gate calls (exactly one
per submitted form), DB writes, Python peak memory and AI calls (0)."""

import os
import time
import tracemalloc
import uuid

import pytest
from sqlalchemy import event

from app.career.database.models import TenantRow
from app.career.importer import SeedImporter
from app.career.models import AnswerBankEntryCreate, AnswerStatus
from app.career.repository import EvidenceRepository
from app.config import settings
from app.database import get_engine, get_session_factory
from app.execution.service import ExecutionService
from app.pipeline.models import QueueAction
from tests.discovery.conftest import WriteCounter
from tests.execution.extension_driver import ExtensionDriver
from tests.execution.playwright_conftest import (
    LEVER_BANK,
    fixture_url,
    make_session,
    requires_browser,
)
from tests.execution.test_perf import _cleanup, _seed
from tests.preparation.conftest import FACT_ANSWERS

pytestmark = requires_browser

#: 10-form mix: 6 Greenhouse success, 1 Lever success, 1 validation error,
#: 1 CAPTCHA page (handoff before any fill), 1 ambiguous post-submit state.
MIX = (
    [("greenhouse.html", "GREENHOUSE", "success")] * 6
    + [("lever.html", "LEVER", "success")]
    + [("greenhouse.html?mode=validation", "GREENHOUSE", "validation")]
    + [("captcha.html", "CAREERS", "captcha")]
    + [("greenhouse.html?mode=hang", "GREENHOUSE", "unknown")]
)


class _Harness:
    def __init__(self, session, tenant_id, service):
        self.session, self.tenant_id, self.service = session, tenant_id, service

    def item(self, attempt):
        return self.service.queue.find(attempt.opportunity_id, QueueAction.SUBMIT)


def _benchmark(n: int, tmp_path) -> dict:
    session = get_session_factory()()
    tenant_id = f"perf-p9-{uuid.uuid4().hex[:6]}"
    counter = WriteCounter()
    engine = get_engine()
    browser = make_session()
    previous_root = settings.documents_root
    settings.documents_root = str(tmp_path / "documents")
    try:
        session.add(TenantRow(id=tenant_id, name="perf9"))
        session.commit()
        repo = EvidenceRepository(session, tenant_id)
        SeedImporter(repo).import_file(settings.career_data_path)
        repo.upsert_profile({"email": "perf@example.com", "phone": "+91 90000 00000"}, None, "perf")
        for category, question, answer in FACT_ANSWERS + LEVER_BANK:
            if repo.find_answer(question) is None:
                repo.create_answer(AnswerBankEntryCreate(category=category, question=question, answer=answer, status=AnswerStatus.APPROVED), "perf")
        repo.commit()

        def url_for(i, kind):
            page, source, _ = MIX[i % len(MIX)]
            base, _, query = page.partition("?")
            return fixture_url(base) + ("?" + query if query else ""), source

        _seed(session, tenant_id, n, url_for=url_for, plain=True)
        expected: dict[str, int] = {}
        for i in range(n):
            expected[MIX[i % len(MIX)][2]] = expected.get(MIX[i % len(MIX)][2], 0) + 1

        service = ExecutionService(session, tenant_id, actor="perf", executors={})
        harness = _Harness(session, tenant_id, service)
        attempts = service.ready(n)
        assert len(attempts) == n
        event.listen(engine, "after_cursor_execute", counter)
        tracemalloc.start()
        started = time.perf_counter()
        gate_calls = 0
        fill_seconds = 0.0
        outcomes: dict[str, int] = {}
        for attempt in attempts:
            with browser.page() as page:
                ext = ExtensionDriver(harness, page, worker="ext-perf", mode="auto", wait_ms=1500)
                t0 = time.perf_counter()
                phase = ext.fill(attempt)
                fill_seconds += time.perf_counter() - t0
                if ext.phase == "submitted" or phase == "filled":
                    ext.wait(timeout_s=6)
                gate_calls += len(ext.gate_calls)
                if ext.report_outcome:
                    outcomes[ext.report_outcome["outcome"]] = outcomes.get(ext.report_outcome["outcome"], 0) + 1
                elif phase == "handoff":
                    outcomes["HANDOFF"] = outcomes.get("HANDOFF", 0) + 1
        seconds = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        event.remove(engine, "after_cursor_execute", counter)

        by_status = service.summary().attempts_by_status
        assert by_status.get("VERIFIED") == expected["success"], (by_status, outcomes)
        assert by_status.get("UNCERTAIN") == expected["unknown"]
        assert by_status.get("BLOCKED") == expected["captcha"]
        assert by_status.get("NEEDS_REVIEW") == expected["validation"]
        assert gate_calls == expected["success"] + expected["validation"] + expected["unknown"], "exactly one gate per submitted form"
        results = {
            "forms": n,
            "seconds": round(seconds, 2),
            "seconds_per_form": round(seconds / n, 2),
            "forms_per_minute": round(n / max(seconds, 1e-6) * 60),
            "browser_launches": browser.launches,
            "claim_start_discover_map_fill_seconds": round(fill_seconds, 2),
            "gate_calls": gate_calls,
            "outcomes": outcomes,
            "attempts_by_status": by_status,
            "db_writes": counter.writes,
            "db_selects": counter.selects,
            "python_peak_memory_mb": round(peak / 1024 / 1024, 1),
            "ai_calls": 0,
        }
        print("\nEXTENSION BENCHMARK:", results)
        return results
    finally:
        settings.documents_root = previous_root
        browser.close()
        _cleanup(session, tenant_id)
        session.close()


@pytest.mark.perf
def test_extension_flow_50_local_forms(tmp_path):
    results = _benchmark(50, tmp_path)
    assert results["ai_calls"] == 0 and results["browser_launches"] == 1


@pytest.mark.perf
@pytest.mark.skipif(os.environ.get("CAREEROS_PERF") != "1", reason="set CAREEROS_PERF=1 for the 200-form run")
def test_extension_flow_200_local_forms(tmp_path):
    results = _benchmark(200, tmp_path)
    assert results["ai_calls"] == 0
