"""Local browser benchmark: 100 application forms (500 opt-in) with mixed outcomes.

Everything is a local ``file://`` fixture; no network. Measures wall time,
browser lifecycle overhead, form discovery time, DB writes, Python peak
memory, and AI calls (always 0: execution consumes prepared packages).
"""

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
from app.execution.models import ExecutorKind
from app.execution.playwright import discovery as discovery_module
from app.execution.playwright import executor as executor_module
from app.execution.service import ExecutionService
from tests.discovery.conftest import WriteCounter
from tests.execution.playwright_conftest import (
    LEVER_BANK,
    fixture_url,
    make_executor,
    make_session,
    requires_browser,
)
from tests.execution.test_perf import _cleanup, _seed
from tests.preparation.conftest import FACT_ANSWERS

pytestmark = requires_browser

#: 100-form mix: 60 Greenhouse success, 10 Lever success, 5 Ashby success,
#: 5 validation errors, 5 CAPTCHA pages, 5 login walls, 5 MFA prompts,
#: 5 ambiguous post-submit states.
MIX = (
    [("greenhouse.html", "GREENHOUSE", "success")] * 12
    + [("lever.html", "LEVER", "success")] * 2
    + [("ashby.html", "ASHBY", "success")] * 1
    + [("greenhouse.html?mode=validation", "GREENHOUSE", "validation")]
    + [("captcha.html", "CAREERS", "captcha")]
    + [("login.html", "CAREERS", "auth")]
    + [("mfa.html", "CAREERS", "mfa")]
    + [("greenhouse.html?mode=hang", "GREENHOUSE", "unknown")]
)


def _benchmark(n: int) -> dict:
    session = get_session_factory()()
    tenant_id = f"perf-p7-{uuid.uuid4().hex[:6]}"
    counter = WriteCounter()
    engine = get_engine()
    browser = make_session()
    resume = None
    try:
        session.add(TenantRow(id=tenant_id, name="perf7"))
        session.commit()
        repo = EvidenceRepository(session, tenant_id)
        SeedImporter(repo).import_file(settings.career_data_path)
        repo.upsert_profile({"email": "perf@example.com", "phone": "+91 90000 00000"}, None, "perf")
        # The seeded preparations answer only the standard "why this role"
        # question; the Ashby-like fixture also asks "Tell us about yourself".
        extra_bank = [("about_you", "Tell us about yourself.", "Final-year CSE student with Python/Go backend projects (Ticket Engine, CareerOS).")]
        for category, question, answer in FACT_ANSWERS + LEVER_BANK + extra_bank:
            if repo.find_answer(question) is None:
                repo.create_answer(AnswerBankEntryCreate(category=category, question=question, answer=answer, status=AnswerStatus.APPROVED), "perf")
        repo.commit()
        import tempfile

        resume = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
        resume.write("Ribhu Siripurapu - resume (benchmark file)\n")
        resume.close()

        def url_for(i, kind):
            page, source, _ = MIX[i % len(MIX)]
            base, _, query = page.partition("?")
            return fixture_url(base) + ("?" + query if query else ""), source

        script, expected = _seed(session, tenant_id, n, url_for=url_for, plain=True)
        expected_outcomes: dict[str, int] = {}
        for i in range(n):
            expected_outcomes[MIX[i % len(MIX)][2]] = expected_outcomes.get(MIX[i % len(MIX)][2], 0) + 1

        executor = make_executor(browser, resume.name, dry_run=False, submit_wait_ms=1500)
        service = ExecutionService(session, tenant_id, actor="perf", executors={ExecutorKind.PLAYWRIGHT_LOCAL: executor})
        scan_time = {"seconds": 0.0, "calls": 0}
        original_scan = discovery_module.scan_page

        def timed_scan(page):
            started = time.perf_counter()
            try:
                return original_scan(page)
            finally:
                scan_time["seconds"] += time.perf_counter() - started
                scan_time["calls"] += 1

        executor_module.scan_page = timed_scan
        event.listen(engine, "after_cursor_execute", counter)
        tracemalloc.start()
        started = time.perf_counter()
        totals: dict[str, int] = {}
        for _ in range(n // 10 + 2):
            counts = service.run_queue("perf-browser", limit=10, executor_kind=ExecutorKind.PLAYWRIGHT_LOCAL)
            if not counts.get("claimed"):
                break
            for k, v in counts.items():
                totals[k] = totals.get(k, 0) + v
        seconds = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        event.remove(engine, "after_cursor_execute", counter)
        executor_module.scan_page = original_scan

        by_status = service.summary().attempts_by_status
        assert totals["claimed"] >= n
        assert executor.submit_clicks == expected_outcomes["success"] + expected_outcomes["validation"] + expected_outcomes["unknown"]
        assert by_status.get("VERIFIED") == expected_outcomes["success"]
        assert by_status.get("UNCERTAIN") == expected_outcomes["unknown"]
        assert by_status.get("BLOCKED") == expected_outcomes["captcha"] + expected_outcomes["auth"] + expected_outcomes["mfa"]
        assert by_status.get("NEEDS_REVIEW") == expected_outcomes["validation"]
        results = {
            "forms": n,
            "seconds": round(seconds, 2),
            "seconds_per_form": round(seconds / n, 2),
            "forms_per_minute": round(n / max(seconds, 1e-6) * 60),
            "browser_launches": browser.launches,
            "form_discovery_seconds": round(scan_time["seconds"], 2),
            "form_discovery_calls": scan_time["calls"],
            "outcomes": {k: v for k, v in totals.items() if k != "claimed"},
            "attempts_by_status": by_status,
            "submit_clicks": executor.submit_clicks,
            "db_writes": counter.writes,
            "db_selects": counter.selects,
            "python_peak_memory_mb": round(peak / 1024 / 1024, 1),
            "ai_calls": 0,
        }
        print("\nPLAYWRIGHT BENCHMARK:", results)
        return results
    finally:
        browser.close()
        if resume is not None:
            try:
                os.unlink(resume.name)
            except OSError:
                pass
        _cleanup(session, tenant_id)
        session.close()


@pytest.mark.perf
def test_execute_100_local_forms():
    results = _benchmark(100)
    assert results["ai_calls"] == 0 and results["browser_launches"] == 1


@pytest.mark.perf
@pytest.mark.skipif(os.environ.get("CAREEROS_PERF") != "1", reason="set CAREEROS_PERF=1 for the 500-form run")
def test_execute_500_local_forms():
    results = _benchmark(500)
    assert results["ai_calls"] == 0
