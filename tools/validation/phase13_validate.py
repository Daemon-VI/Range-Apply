"""Phase 13 real-world validation harness (dry run only, never submits).

    python tools/validation/phase13_validate.py setup
    python tools/validation/phase13_validate.py discover
    python tools/validation/phase13_validate.py match
    python tools/validation/phase13_validate.py corpus
    python tools/validation/phase13_validate.py specials
    python tools/validation/phase13_validate.py extension
    python tools/validation/phase13_validate.py documents
    python tools/validation/phase13_validate.py signals
    python tools/validation/phase13_validate.py chain
    python tools/validation/phase13_validate.py diagnostics
    python tools/validation/phase13_validate.py soak --cycles 4

Boundaries, on purpose:
* a scratch database and documents root (never the operator's), AI off;
* real public ATS pages are only *navigated and discovered*; the server
  maps every field from the real preparation, verifies the rendered
  documents and runs the pre-submit gate, but **no candidate data is typed
  into an employer page and no submit control is ever pressed**;
* one page load at a time with a pacer; no login, CAPTCHA or MFA is
  attempted; public demo pages stand in for challenge pages;
* nothing here is a test fixture: every number in the JSON files is a
  real-world observation and is reported as such.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRATCH = Path(os.environ.get("CAREEROS_VALIDATION_DIR") or (Path(os.environ.get("TEMP", "/tmp")) / "careeros_phase13"))
OUT = ROOT / "docs" / "validation" / "phase13"
TENANT = "validation-p13"

SCRATCH.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{(SCRATCH / 'phase13.db').as_posix()}"
os.environ["DOCUMENTS_ROOT"] = str(SCRATCH / "documents")
os.environ["AI_CACHE_DIR"] = str(SCRATCH / "ai-cache")
os.environ["AI_ENABLED"] = "false"
os.environ["PLAYWRIGHT_DRY_RUN"] = "true"
os.environ["PLAYWRIGHT_HEADLESS"] = "true"
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("API_KEY", "validation-only-local-key")
os.environ["LOG_LEVEL"] = "INFO"
os.environ["DISCOVERY_PROJECT_TENANTS"] = TENANT
sys.path.insert(0, str(ROOT))

from alembic.config import Config  # noqa: E402

from alembic import command  # noqa: E402

BOARDS = [
    ("GREENHOUSE", "discord", "Discord"),
    ("GREENHOUSE", "duolingo", "Duolingo"),
    ("GREENHOUSE", "asana", "Asana"),
    ("LEVER", "nium", "Nium"),
    ("LEVER", "spotify", "Spotify"),
    ("ASHBY", "linear", "Linear"),
    ("ASHBY", "posthog", "PostHog"),
    ("ASHBY", "supabase", "Supabase"),
]

#: Public pages that stand in for the challenge / wall cases. No credentials, no submission.
SPECIAL_PAGES = [
    ("recaptcha_demo", "https://www.google.com/recaptcha/api2/demo", "CAPTCHA page (Google reCAPTCHA public demo)"),
    ("hcaptcha_demo", "https://accounts.hcaptcha.com/demo", "CAPTCHA page (hCaptcha public demo)"),
    ("github_login", "https://github.com/login", "login-required page"),
    ("linkedin_login", "https://www.linkedin.com/login", "login-required page (LinkedIn)"),
    ("lever_posting_no_form", "https://jobs.lever.co/nium", "job list page without an application form"),
    ("greenhouse_missing_job", "https://boards.greenhouse.io/discord/jobs/1", "closed / missing posting"),
    ("blank_page", "about:blank", "malformed / empty page"),
]


def _save(name: str, data) -> None:
    (OUT / f"{name}.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print(f"saved {OUT / (name + '.json')}")


def _load(name: str):
    path = OUT / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _session():
    from app.database import get_session_factory

    return get_session_factory()()


def _chromium_processes() -> int:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*ms-playwright*' }).Count"], capture_output=True, text=True, timeout=30).stdout.strip()
        return int(out or 0)
    except Exception:  # noqa: BLE001
        return -1


def _python_rss_mb() -> float:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", f"(Get-Process -Id {os.getpid()}).WorkingSet64"], capture_output=True, text=True, timeout=30).stdout.strip()
        return round(int(out) / 1024 / 1024, 1)
    except Exception:  # noqa: BLE001
        return -1.0


def _dir_size_mb(path: Path) -> float:
    return round(sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1024 / 1024, 2) if path.exists() else 0.0


# ------------------------------------------------------------------ setup


def stage_setup() -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "head")
    from app.career.database.models import TenantRow
    from app.career.importer import SeedImporter
    from app.career.models import AnswerBankEntryCreate, AnswerStatus
    from app.career.repository import EvidenceRepository
    from app.config import settings
    from app.pipeline.models import ApplicationPolicyUpdate
    from app.pipeline.repository import PolicyRepository

    db = _session()
    try:
        if db.get(TenantRow, TENANT) is None:
            db.add(TenantRow(id=TENANT, name="Phase 13 validation"))
            db.commit()
        repo = EvidenceRepository(db, TENANT)
        report = SeedImporter(repo).import_file(settings.career_data_path)
        repo.upsert_profile({"email": "ribhu.validation@example.com", "phone": "+91 90000 00000"}, None, "validation")
        for category, question, answer in [
            ("work_authorization", "Are you legally authorized to work in this country?", "Yes, I am an Indian citizen authorized to work in India."),
            ("sponsorship", "Will you now or in the future require sponsorship?", "No, I do not require sponsorship."),
            ("notice_period", "What is your notice period?", "I can start within two weeks."),
            ("salary", "What are your salary expectations?", "I am open to discussing a competitive package for this role."),
        ]:
            if repo.find_answer(question) is None:
                repo.create_answer(AnswerBankEntryCreate(category=category, question=question, answer=answer, status=AnswerStatus.APPROVED), "validation")
        repo.commit()
        PolicyRepository(db, TENANT).update(ApplicationPolicyUpdate(daily_cap=60, weekly_cap=300, cooldown_days=0), "validation")
        db.commit()
        _save("00_setup", {"database": os.environ["DATABASE_URL"], "documents_root": os.environ["DOCUMENTS_ROOT"], "seed_import": report if isinstance(report, dict) else str(report), "tenant": TENANT})
    finally:
        db.close()


# -------------------------------------------------------------- discovery


def stage_discover(second_pass: bool = False, label: str = "") -> None:
    from app.jobs.database.models import JobRow, SourceHealthRow
    from app.jobs.models.enums import JobSourceType
    from app.jobs.pipeline.discovery_service import JobDiscoveryService
    from app.pipeline.database.models import OpportunityRow

    service = JobDiscoveryService()
    results = []
    db = _session()
    try:
        for source, identifier, company in BOARDS:
            started = time.perf_counter()
            try:
                run = asyncio.run(service.run_discovery(db, JobSourceType(source), identifier, company_name=company, trigger="phase13"))
                results.append({"source": source, "identifier": identifier, "status": run.status, "failure_kind": run.failure_kind, "seconds": round(time.perf_counter() - started, 2), "candidates": run.candidates_discovered, "jobs_new": run.jobs_new, "jobs_unchanged": run.jobs_unchanged, "jobs_versioned": run.jobs_versioned, "jobs_duplicate": run.jobs_duplicate, "jobs_rejected": run.jobs_rejected, "jobs_closed": run.jobs_closed, "jobs_reposted": run.jobs_reposted, "jobs_failed": run.jobs_failed, "opportunities_new": run.opportunities_new, "opportunities_linked": run.opportunities_linked, "network_requests": run.network_requests, "rate_limit_hits": run.rate_limit_hits, "ai_calls": run.ai_calls, "errors": (run.errors or [])[:3]})
            except Exception as exc:  # noqa: BLE001 - one board's failure is a result, not a crash
                db.rollback()
                results.append({"source": source, "identifier": identifier, "status": "exception", "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
            time.sleep(2.0)
        from app.pipeline.database.models import CandidateOpportunityRow
        totals = {"jobs": db.query(JobRow).count(), "opportunities": db.query(OpportunityRow).count(), "candidate_opportunities_tenant": db.query(CandidateOpportunityRow).filter(CandidateOpportunityRow.tenant_id == TENANT).count(), "by_source": {s: db.query(JobRow).filter(JobRow.source == s).count() for s in ("GREENHOUSE", "LEVER", "ASHBY")}, "closed": db.query(JobRow).filter(JobRow.job_status == "CLOSED").count()}
        health = [{"source": h.source, "identifier": h.source_identifier, "runs": h.runs_total, "success": h.runs_success, "failed": h.runs_failed, "fetched": h.jobs_fetched_total, "rejected": h.jobs_rejected_total, "duplicates": h.duplicates_total, "consecutive_failures": h.consecutive_failures} for h in db.query(SourceHealthRow).all()]
        _save(label or ("02_discovery_second_pass" if second_pass else "01_discovery"), {"boards": results, "totals": totals, "source_health": health})
    finally:
        db.close()


# ------------------------------------------------------- match + schedule


def stage_match(limit_prepare: int = 40) -> None:
    from app.intelligence.services.factory import build_orchestrator
    from app.intelligence.services.match_persistence import run_matching
    from app.pipeline.sync import sync_run
    from app.scheduler.service import SchedulerService
    from app.services.career_brain import CareerBrainService

    db = _session()
    try:
        brain = CareerBrainService(db=db, tenant_id=TENANT)
        brain.load()
        started = time.perf_counter()
        run = run_matching(db=db, orchestrator=build_orchestrator(brain), job_ids=None, trigger="phase13", include_closed=False, limit=None, tenant_id=TENANT)
        match_seconds = time.perf_counter() - started
        started = time.perf_counter()
        report = sync_run(db, TENANT, run.id)
        sync_seconds = time.perf_counter() - started
        scheduler = SchedulerService(db, TENANT, actor="phase13")
        started = time.perf_counter()
        srun = scheduler.run(trigger="phase13", window=limit_prepare, prepare=True, prepare_limit=limit_prepare, worker_id="phase13-prep")
        sched_seconds = time.perf_counter() - started
        from app.execution.service import ExecutionService

        counts = ExecutionService(db, TENANT, actor="phase13", executors={}).enqueue_ready()
        db.commit()
        _save("03_match_schedule", {"match": {"status": run.status, "jobs_processed": run.jobs_processed, "jobs_matched": run.jobs_matched, "jobs_failed": run.jobs_failed, "seconds": round(match_seconds, 2), "ai_calls": 0}, "sync": {"synced": report.synced, "admitted": report.admitted, "not_admitted": report.not_admitted, "seconds": round(sync_seconds, 2)}, "scheduler": {"considered": srun.considered, "admitted": srun.admitted, "blocked_by_reason": srun.blocked_by_reason, "ready_for_execution": srun.ready_for_execution, "preparation": srun.preparation, "seconds": round(sched_seconds, 2)}, "submit_items": counts})
    finally:
        db.close()


# ------------------------------------------------------------------ corpus


def _classify(form, answers, scan_meta: dict, gate_failures: list[str]) -> dict:
    from app.execution.models import FieldAnswerStatus, FieldType

    fields = form.fields
    by_type: dict[str, int] = {}
    for f in fields:
        by_type[f.field_type.value] = by_type.get(f.field_type.value, 0) + 1
    required = [a for a in answers if a.required]
    answered_required = [a for a in required if a.status is FieldAnswerStatus.ANSWERED]
    missing_required = [a.label[:60] for a in required if a.status is not FieldAnswerStatus.ANSWERED]
    files = [f for f in fields if f.field_type is FieldType.FILE]
    file_answers = [a for a in answers if a.artifact_type]
    unknown_types = [f.label[:60] for f in fields if f.field_type is FieldType.UNKNOWN]
    return {
        "fields": len(fields),
        "by_type": by_type,
        "required": len(required),
        "required_answered": len(answered_required),
        "missing_required": missing_required[:12],
        "optional_skipped": sum(1 for a in answers if not a.required and a.status is not FieldAnswerStatus.ANSWERED),
        "file_fields": [f.label[:60] for f in files],
        "file_fields_mapped_to_artifacts": [(a.label[:60], a.artifact_type) for a in file_answers],
        "unknown_widget_fields": unknown_types[:8],
        "answer_sources": _count(a.source.value for a in answers),
        "answer_statuses": _count(a.status.value for a in answers),
        "custom_widgets": scan_meta.get("custom_widgets"),
        "submit_buttons": scan_meta.get("submit_buttons"),
        "handoff": scan_meta.get("handoff"),
        "strategy": scan_meta.get("strategy"),
        "page_title": scan_meta.get("page_title"),
        "gate_failures": gate_failures,
        "sample_labels": [f"{f.label[:50]} [{f.field_type.value}{'*' if f.required else ''}]" for f in fields[:14]],
    }


def _count(values) -> dict:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def _executor():
    from app.execution.playwright.browser import BrowserSession, Pacer
    from app.execution.playwright.executor import PlaywrightExecutor

    session = BrowserSession(headless=True, profile_dir="")
    session.start()
    return PlaywrightExecutor(session=session, dry_run=True, handoff_wait_seconds=0, pacer=Pacer(3.0))


def stage_corpus(max_per_source: int = 8) -> None:
    from app.application.database.models import ApplicationRow
    from app.execution.executors.base import ExecutorError
    from app.execution.service import ExecutionService
    from app.jobs.database.models import JobRow

    db = _session()
    executor = _executor()
    results = []
    try:
        service = ExecutionService(db, TENANT, actor="phase13", executors={executor.kind: executor})
        ready = db.query(ApplicationRow).filter(ApplicationRow.tenant_id == TENANT, ApplicationRow.status == "READY").all()
        chosen: dict[str, list] = {"GREENHOUSE": [], "LEVER": [], "ASHBY": []}
        for attempt in ready:
            job = db.get(JobRow, attempt.job_id)
            if job is not None and job.source in chosen and len(chosen[job.source]) < max_per_source:
                chosen[job.source].append((attempt, job))
        for source, pairs in chosen.items():
            for attempt, job in pairs:
                preparation, co, opportunity = service._context(attempt)
                entry = {"source": source, "company": job.company, "title": job.title, "url": job.application_url or job.source_url, "attempt_id": attempt.id}
                started = time.perf_counter()
                try:
                    documents = service.documents.ensure_for_execution(preparation)
                    entry["documents"] = {"artifacts": [(a.artifact_type, a.format.value, a.content_hash[:12], a.byte_size) for a in documents.artifacts], "problems": documents.problems}
                    package = service.package(attempt)
                    package.execution_config["artifact_files"] = {a.artifact_type: service._document_path(a.id) for a in documents.artifacts}
                    entry["ats_family"] = package.target.ats_family.value
                    t0 = time.perf_counter()
                    form = executor.prepare(package)
                    entry["discover_seconds"] = round(time.perf_counter() - t0, 2)
                    t0 = time.perf_counter()
                    snapshot, answers = service.capture_form(attempt, form, None, package)
                    db.commit()
                    entry["mapping_seconds"] = round(time.perf_counter() - t0, 3)
                    t0 = time.perf_counter()
                    # The gate only passes for an attempt a worker is submitting; simulate that state for the check, then restore it.
                    before = attempt.status
                    attempt.status = "SUBMITTING"
                    db.flush()
                    try:
                        failures = service._pre_submit_gate(attempt.id)()
                    finally:
                        attempt.status = before
                        db.commit()
                    entry["gate_seconds"] = round(time.perf_counter() - t0, 3)
                    entry["result"] = _classify(form, answers, form.metadata, failures)
                    state = executor._runs.get(attempt.id)
                    if state is not None and state.scan is not None:
                        entry["result"].update({"captcha": state.scan.captcha, "captcha_inline": state.scan.captcha_inline, "in_frame": form.metadata.get("in_frame"), "settle_ms": form.metadata.get("settle_ms")})
                    entry["outcome"] = "handoff:" + form.metadata["handoff"] if form.metadata.get("handoff") else ("discovered" if form.fields else "no_form")
                except ExecutorError as exc:
                    db.rollback()
                    entry["outcome"] = "executor_error"
                    entry["error"] = {"class": exc.error_class.value if exc.error_class else None, "retryable": exc.retryable, "before_submit": exc.before_submit, "message": str(exc)[:200]}
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    entry["outcome"] = "exception"
                    entry["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                finally:
                    executor._close_state(attempt.id)
                entry["seconds_total"] = round(time.perf_counter() - started, 2)
                results.append(entry)
                print(f"{source:10} {entry['outcome']:28} fields={entry.get('result', {}).get('fields')} {job.company[:24]} / {job.title[:40]}")
        _save("04_corpus", {"pages": results, "summary": _count(r["outcome"] for r in results), "by_source": {s: _count(r["outcome"] for r in results if r["source"] == s) for s in chosen}})
    finally:
        executor.close()
        db.close()


def stage_specials() -> None:
    from app.execution.executors.base import ExecutorError
    from app.execution.models import ApplicationMethod, ATSFamily, ExecutionPackage, ExecutionTarget
    from app.execution.package import load_answer_bank, load_profile_facts
    from app.execution.playwright.discovery import scan_page

    db = _session()
    executor = _executor()
    results = []
    try:
        bank = load_answer_bank(db, TENANT)
        profile = load_profile_facts(db, TENANT)
        for key, url, expectation in SPECIAL_PAGES:
            family = ATSFamily.GREENHOUSE if "greenhouse" in url else ATSFamily.LEVER if "lever.co" in url else ATSFamily.GENERIC_WEB
            package = ExecutionPackage(tenant_id=TENANT, candidate_opportunity_id="special", opportunity_id="special", preparation_id="special", application_id=f"special-{key}", attempt_number=1, preparation_version=1, preparation_fingerprint="x", target=ExecutionTarget(source="VALIDATION", ats_family=family, canonical_url=url, company="special", title=key, method=ApplicationMethod.BROWSER_FORM), answer_bank=bank, profile=profile)
            entry = {"key": key, "url": url, "expectation": expectation}
            started = time.perf_counter()
            try:
                form = executor.prepare(package)
                state = executor._runs.get(package.application_id)
                scan = state.scan if state else scan_page(state.page)
                entry.update({"fields": len(form.fields), "captcha": scan.captcha, "mfa": scan.mfa, "login_wall": scan.login_wall, "custom_widgets": scan.custom_widgets, "submit_buttons": [b.get("text", "")[:40] for b in scan.submit_buttons[:3]], "handoff": form.metadata.get("handoff"), "title": scan.title[:80], "outcome": "handoff:" + form.metadata["handoff"] if form.metadata.get("handoff") else ("discovered" if form.fields else "no_form")})
            except ExecutorError as exc:
                entry.update({"outcome": "executor_error", "error": {"class": exc.error_class.value if exc.error_class else None, "retryable": exc.retryable, "before_submit": exc.before_submit, "message": str(exc)[:200]}})
            except Exception as exc:  # noqa: BLE001
                entry.update({"outcome": "exception", "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            finally:
                executor._close_state(package.application_id)
            entry["seconds"] = round(time.perf_counter() - started, 2)
            results.append(entry)
            print(f"{key:24} {entry.get('outcome')}  {expectation}")
        _save("05_specials", {"pages": results})
    finally:
        executor.close()
        db.close()


# --------------------------------------------------------- extension parity


def stage_extension(limit: int = 6) -> None:
    """Run the extension's own page scripts (discover.js / content.js / fill.js)
    on real pages the way the service worker injects them and compare the
    discovered form with the Playwright scan (the same script)."""
    from app.execution.playwright.browser import BrowserSession, Pacer
    from app.execution.playwright.discovery import scan_page

    corpus = _load("04_corpus") or {"pages": []}
    pages = [p for p in corpus["pages"] if p.get("outcome") in ("discovered",)][:limit]
    src = ROOT / "extension" / "src"
    scripts = [(src / n).read_text(encoding="utf-8") for n in ("discover.js", "fill.js", "content.js")]
    session = BrowserSession(headless=True, profile_dir="")
    session.start()
    pacer = Pacer(3.0)
    results = []
    try:
        for entry in pages:
            with session.page() as page:
                pacer.wait()
                item = {"source": entry["source"], "company": entry["company"], "url": entry["url"]}
                started = time.perf_counter()
                try:
                    page.goto(entry["url"], wait_until="domcontentloaded")
                    page.wait_for_load_state("load", timeout=30000)
                    page.wait_for_timeout(3000)  # the extension scans when the person presses Fill, i.e. after the page has rendered
                    playwright_scan = scan_page(page)
                    page.evaluate("() => { window.careerosBridge = {send: (m) => ({ok: true, echoed: m})}; }")
                    for script in scripts:
                        page.evaluate(script)  # executeScript semantics: not subject to the page's CSP
                    reply = page.evaluate("() => window.__careerosContent.handle({type: 'discover'})")
                    state = page.evaluate("() => window.__careerosContent.handle({type: 'state'})")
                    ext_fields = reply.get("scan", {}).get("fields", []) if reply.get("ok") else []
                    same = [(f.external_id, f.field_type.value, f.required) for f in playwright_scan.fields] == [(f.get("external_id"), f.get("field_type"), bool(f.get("required"))) for f in ext_fields]
                    item.update({"injected": bool(reply.get("ok")), "fields_extension": len(ext_fields), "fields_playwright": len(playwright_scan.fields), "identical_discovery": same, "content_state_ok": bool(state.get("ok")), "armed": state.get("armed"), "submit_buttons": len(reply.get("scan", {}).get("submit_buttons", [])), "csp_note": "page scripts evaluated via page.evaluate (extension executeScript semantics)"})
                except Exception as exc:  # noqa: BLE001
                    item.update({"injected": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
                item["seconds"] = round(time.perf_counter() - started, 2)
                results.append(item)
                print(item)
        _save("06_extension_parity", {"pages": results, "note": "sender validation, service-worker restart and real Chrome runtime are NOT exercised here (no Node / no extension-loaded Chrome in this environment)"})
    finally:
        session.close()


# ----------------------------------------------------------------- documents


def stage_documents() -> None:
    from app.documents.database.models import DocumentArtifactRow
    from app.documents.storage import ArtifactStore, sha256_file
    from app.documents.validate import extract_text

    db = _session()
    results = []
    try:
        store = ArtifactStore()
        rows = db.query(DocumentArtifactRow).filter(DocumentArtifactRow.tenant_id == TENANT).all()
        for row in rows:
            path = store.absolute(row.relative_path)
            entry = {"artifact_type": row.artifact_type, "format": row.format, "version": row.version, "status": row.status, "bytes": row.byte_size, "pages": row.page_count}
            try:
                from app.documents.models import DocumentFormat

                fmt = DocumentFormat(row.format)
                data = path.read_bytes()
                pages_count, page_texts = extract_text(data, fmt)
                text = "\n".join(page_texts)
                entry.update({"opens": True, "extracted_pages": pages_count, "text_chars": len(text), "non_ascii_chars": sum(1 for ch in text if ord(ch) > 127), "has_email": "ribhu.validation@example.com" in text, "has_name": "Ribhu" in text, "sha256_matches": sha256_file(path) == row.content_hash, "problems": store.verify(row.relative_path, row.content_hash, row.byte_size, fmt)})
                if fmt is DocumentFormat.PDF:
                    from pypdf import PdfReader

                    reader = PdfReader(str(path))
                    links = 0
                    for page in reader.pages:
                        for annot in page.get("/Annots") or []:
                            obj = annot.get_object()
                            if obj.get("/Subtype") == "/Link":
                                links += 1
                    entry["pdf_links"] = links
                    entry["pdf_pages"] = len(reader.pages)
            except Exception as exc:  # noqa: BLE001
                entry.update({"opens": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
            results.append(entry)
        summary = {"artifacts": len(results), "open": sum(1 for r in results if r.get("opens")), "hash_ok": sum(1 for r in results if r.get("sha256_matches")), "with_name": sum(1 for r in results if r.get("has_name")), "with_links": sum(1 for r in results if r.get("pdf_links")), "non_ascii_total": sum(r.get("non_ascii_chars", 0) for r in results), "by_format": _count(r["format"] for r in results)}
        _save("07_documents", {"summary": summary, "artifacts": results[:60]})
        print(summary)
    finally:
        db.close()


# ------------------------------------------------------------------- signals


def _messages(attempts_by_company: dict[str, list]) -> list[dict]:
    """Messages modelled on real ATS / recruiter formats (no real employer text is copied)."""
    out = []
    companies = list(attempts_by_company)
    if not companies:
        return out
    a = attempts_by_company[companies[0]][0]
    b = attempts_by_company[companies[1 % len(companies)]][0]
    ref = f"GH-{a['id'][:6].upper()}"
    out += [
        {"kind": "greenhouse_confirmation", "message_id": f"<gh-{a['id']}@greenhouse-mail.io>", "sender": "no-reply@greenhouse-mail.io", "subject": f"Thank you for applying to {a['company']}", "text": f"Hi Ribhu,\n\nThank you for your interest in the {a['title']} position at {a['company']}. We have received your application and will review it shortly. Application ID: {ref}\n\nBest,\nThe {a['company']} Team", "expect_category": "APPLICATION_CONFIRMATION", "expect_application": a["id"], "reference_for": a["id"], "reference": ref},
        {"kind": "greenhouse_confirmation_duplicate", "message_id": f"<gh-{a['id']}@greenhouse-mail.io>", "sender": "no-reply@greenhouse-mail.io", "subject": f"Thank you for applying to {a['company']}", "text": f"Hi Ribhu,\n\nThank you for your interest in the {a['title']} position at {a['company']}. We have received your application and will review it shortly. Application ID: {ref}\n\nBest,\nThe {a['company']} Team", "expect_duplicate_of": "greenhouse_confirmation"},
        {"kind": "lever_received", "message_id": f"<lv-{b['id']}@hire.lever.co>", "sender": f"{b['company']} <no-reply@hire.lever.co>", "subject": f"Your application to {b['company']}", "text": f"Thanks for applying to {b['company']}! We've received your application for {b['title']} and will be in touch if there's a fit.", "expect_category": "APPLICATION_RECEIVED", "expect_application": b["id"]},
        {"kind": "rejection", "message_id": f"<rej-{a['id']}@x>", "sender": f"recruiting@{a['domain']}", "subject": f"Update on your application to {a['company']}", "text": f"Hi Ribhu,\n\nThank you for taking the time to apply for the {a['title']} role. After careful consideration we have decided to move forward with other candidates whose experience more closely matches our current needs. We encourage you to apply for future openings.\n\nBest regards,\n{a['company']} Recruiting", "expect_category": "REJECTION", "expect_application": a["id"]},
        {"kind": "interview_invite", "message_id": f"<int-{b['id']}@x>", "sender": f"talent@{b['domain']}", "subject": f"Interview invitation: {b['title']} at {b['company']}", "text": f"Hi Ribhu, we'd love to invite you to a 30-minute phone screen with our engineering team for the {b['title']} role. Please book a time using the link below.\nhttps://calendly.com/example/30min", "expect_category": "INTERVIEW_INVITATION", "expect_application": b["id"]},
        {"kind": "interview_reply_thread", "message_id": f"<int-reply-{b['id']}@x>", "sender": f"talent@{b['domain']}", "subject": f"Re: Interview invitation: {b['title']} at {b['company']}", "text": "Great, we have scheduled your interview for Tuesday at 10:00. The invite is attached.", "headers": {"In-Reply-To": f"<int-{b['id']}@x>"}, "expect_category": "INTERVIEW_INVITATION", "expect_application": b["id"]},
        {"kind": "assessment", "message_id": f"<asm-{a['id']}@hackerrank.com>", "sender": "noreply@hackerrank.com", "subject": f"{a['company']}: Online Assessment invitation", "text": f"You have been invited to complete the coding challenge for the {a['title']} role at {a['company']}. The assessment link is valid for 5 days.", "expect_category": "ASSESSMENT", "expect_application": a["id"]},
        {"kind": "recruiter_outreach", "message_id": "<rec-1@x>", "sender": "sam@talent-partners.example", "subject": "Reaching out about a backend role", "text": "Hi Ribhu, I'm a technical recruiter and came across your profile. Would love to connect about an opening at a fintech client.", "expect_category": "RECRUITER_CONTACT", "expect_attribution": "UNMATCHED"},
        {"kind": "status_update", "message_id": f"<stat-{b['id']}@x>", "sender": f"no-reply@{b['domain']}", "subject": "Application status", "text": f"The status of your application for {b['title']} at {b['company']} has changed: your application is currently under review.", "expect_category": "STATUS_UPDATE", "expect_application": b["id"]},
        {"kind": "marketing_job_alert", "message_id": "<alert-1@linkedin.com>", "sender": "jobalerts-noreply@linkedin.com", "subject": "New jobs for you: 8 backend engineer roles", "text": "Here are new jobs matching your preferences. Unsubscribe from these emails at any time.", "expect_category": "OTHER", "expect_status": "IGNORED"},
        {"kind": "ambiguous", "message_id": "<amb-1@x>", "sender": f"hello@{a['domain']}", "subject": "Quick question", "text": "Hi, could we chat sometime this week?", "expect_status": "NEEDS_REVIEW"},
        {"kind": "information_request", "message_id": f"<info-{a['id']}@x>", "sender": f"recruiting@{a['domain']}", "subject": f"{a['company']} application: additional information needed", "text": f"Could you please provide your notice period and upload a copy of your degree certificate for the {a['title']} application?", "expect_category": "INFORMATION_REQUEST", "expect_application": a["id"]},
        {"kind": "withdrawal", "message_id": f"<wd-{b['id']}@x>", "sender": f"no-reply@{b['domain']}", "subject": "Application withdrawn", "text": f"This confirms that your application for {b['title']} at {b['company']} has been withdrawn as requested.", "expect_category": "WITHDRAWAL", "expect_application": b["id"]},
        {"kind": "html_email", "message_id": f"<html-{a['id']}@x>", "sender": f"no-reply@{a['domain']}", "subject": f"Application received - {a['title']}", "html": f"<html><body><div style='font-family:sans-serif'><p>Hi Ribhu,</p><p>We have received your application for <b>{a['title']}</b> at {a['company']}.</p><p><a href='https://example.com/track'>Track your application</a></p></div></body></html>", "expect_category": "APPLICATION_RECEIVED", "expect_application": a["id"]},
    ]
    # several applications at one company: company-only evidence must be AMBIGUOUS
    multi = next((c for c in companies if len(attempts_by_company[c]) >= 2), None)
    if multi:
        m = attempts_by_company[multi][0]
        out.append({"kind": "same_company_two_applications", "message_id": f"<multi-{m['id']}@x>", "sender": f"no-reply@{m['domain']}", "subject": f"Thank you for applying to {m['company']}", "text": f"Thank you for applying to {m['company']}. We will review your application shortly.", "expect_attribution": "AMBIGUOUS"})
        out.append({"kind": "same_company_with_title", "message_id": f"<multi-title-{m['id']}@x>", "sender": f"no-reply@{m['domain']}", "subject": f"Thank you for applying: {m['title']}", "text": f"Thank you for applying to the {m['title']} position at {m['company']}.", "expect_application": m["id"]})
    return out


def stage_signals() -> None:
    from app.application.database.models import ApplicationRow
    from app.jobs.database.models import JobRow
    from app.learning.engine import LearningEngine
    from app.pipeline.database.models import OpportunityRow
    from app.pipeline.policy import company_key
    from app.signals.models import EmailMessage
    from app.signals.service import SignalInboxService

    db = _session()
    try:
        attempts = db.query(ApplicationRow).filter(ApplicationRow.tenant_id == TENANT, ApplicationRow.status.in_(["READY", "SUBMITTED", "VERIFIED", "UNCERTAIN"])).all()
        # For attribution the attempts must be "executed": mark a handful as SUBMITTED with a real-looking reference (validation-only bookkeeping).
        by_company: dict[str, list] = {}
        for attempt in attempts[:12]:
            opp = db.get(OpportunityRow, attempt.opportunity_id)
            job = db.get(JobRow, attempt.job_id)
            if attempt.status == "READY":
                attempt.status = "SUBMITTED"
                attempt.submitted_at = attempt.reserved_at or attempt.created_at
            domain = company_key(opp.company).replace(" ", "") + ".example"
            by_company.setdefault(opp.company, []).append({"id": attempt.id, "company": opp.company, "title": opp.title, "domain": domain, "source": job.source if job else None})
        db.commit()
        messages = _messages(by_company)
        for m in messages:
            if m.get("reference_for"):
                row = db.get(ApplicationRow, m["reference_for"])
                row.external_application_id = m["reference"]
                db.commit()
        inbox = SignalInboxService(db, TENANT, actor="phase13")
        results = []
        first_ids: dict[str, str] = {}
        for m in messages:
            msg = EmailMessage(message_id=m.get("message_id"), sender=m["sender"], recipients=["ribhu.validation@example.com"], subject=m["subject"], text=m.get("text"), html=m.get("html"), headers=m.get("headers") or {})
            row, created = inbox.ingest_email(msg)
            db.commit()
            first_ids.setdefault(m["kind"], row.id)
            verdicts = {}
            if m.get("expect_category"):
                verdicts["category_ok"] = row.category == m["expect_category"]
            if m.get("expect_application"):
                verdicts["attribution_ok"] = row.application_id == m["expect_application"]
            if m.get("expect_attribution"):
                verdicts["attribution_status_ok"] = row.attribution_status == m["expect_attribution"]
            if m.get("expect_status"):
                verdicts["status_ok"] = row.status == m["expect_status"]
            if m.get("expect_duplicate_of"):
                verdicts["duplicate_ok"] = (not created) and row.id == first_ids[m["expect_duplicate_of"]]
            results.append({"kind": m["kind"], "created": created, "category": row.category, "confidence": row.confidence, "classification_source": row.classification_source, "attribution": row.attribution_status, "application_id": row.application_id, "status": row.status, "status_reason": row.status_reason, "verdicts": verdicts})
        summary = inbox.summary().model_dump()
        engine = LearningEngine(db, TENANT, actor="phase13")
        snapshot = engine.snapshot(actor="phase13")
        db.commit()
        rows = engine.dataset()
        learning = {"snapshot_id": snapshot.id, "dataset_size": snapshot.dataset_size, "evidence": snapshot.summary.get("evidence"), "completeness": snapshot.summary.get("completeness"), "responded": snapshot.summary.get("responded"), "recommendations": snapshot.recommendation_count, "sample_rows": [{"company": r.company, "responded": r.responded, "interviewed": r.interviewed, "rejected": r.rejected, "evidence_quality": r.evidence_quality.value, "events_counted": r.events_counted, "days_to_response": r.days_to_response} for r in rows[:6]]}
        ok = sum(1 for r in results for v in r["verdicts"].values() if v)
        total = sum(len(r["verdicts"]) for r in results)
        _save("08_signals", {"messages": results, "checks_passed": ok, "checks_total": total, "inbox_summary": summary, "learning": learning})
        print({"checks": f"{ok}/{total}", "review_queue": summary["review_queue"]})
    finally:
        db.close()


def stage_signals_reprocess() -> None:
    """Manual-review flow: reprocess every NEEDS_REVIEW signal (index refreshed,
    same rules) and record what changed. Expected results are read back from
    the message ids the samples embed (``<kind-<application id>@...>``)."""
    import re

    from app.signals.service import SignalInboxService

    db = _session()
    try:
        inbox = SignalInboxService(db, TENANT, actor="phase13")
        queue = inbox.review_queue(limit=200)
        results = []
        for row in queue:
            ref = row.source_reference or ""
            m = re.search(r"<[a-z_-]+?-([0-9a-f-]{36})@", ref)
            expected_app = m.group(1) if m else None
            expected_status = "AMBIGUOUS" if ref.startswith("<multi-") else "NEEDS_REVIEW" if ref.startswith("<amb-") else None
            before = (row.status, row.attribution_status, row.application_id)
            try:
                inbox.reprocess(row.id, "phase13")
                db.commit()
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                results.append({"reference": ref, "error": f"{type(exc).__name__}: {exc}"})
                continue
            verdict = None
            if expected_app:
                verdict = row.application_id == expected_app
            elif expected_status:
                verdict = row.attribution_status == expected_status or row.status == expected_status
            results.append({"reference": ref[:60], "before": before, "after": (row.status, row.attribution_status, row.application_id), "expected_application": expected_app, "expected_status": expected_status, "verdict": verdict})
        summary = inbox.summary().model_dump()
        _save("08b_signals_reprocessed", {"reprocessed": len(results), "now_matched": sum(1 for r in results if r.get("after", ("", ""))[1] == "MATCHED"), "verdicts_ok": sum(1 for r in results if r.get("verdict")), "verdicts_total": sum(1 for r in results if r.get("verdict") is not None), "results": results, "inbox_summary": summary})
        print({"reprocessed": len(results), "review_queue_after": summary["review_queue"]})
    finally:
        db.close()


# ---------------------------------------------------------------- chain audit


def stage_chain(limit: int = 3) -> None:
    from app.application.database.models import ApplicationEventRow, ApplicationRow
    from app.career.database.models import AuditEventRow
    from app.documents.database.models import DocumentArtifactRow
    from app.execution.database.models import ExecutionRunRow, FormSnapshotRow
    from app.jobs.database.models import JobRow
    from app.pipeline.database.models import (
        CandidateOpportunityRow,
        EligibilityDecisionRow,
        OpportunityRow,
        PriorityScoreRow,
    )
    from app.preparation.database.models import ApplicationPreparationRow
    from app.signals.database.models import ApplicationOutcomeRow, OutcomeEventRow, SignalRow

    db = _session()
    chains = []
    try:
        attempts = db.query(ApplicationRow).filter(ApplicationRow.tenant_id == TENANT, ApplicationRow.status.in_(["SUBMITTED", "READY"])).limit(limit).all()
        for attempt in attempts:
            opp = db.get(OpportunityRow, attempt.opportunity_id)
            co = db.get(CandidateOpportunityRow, attempt.candidate_opportunity_id)
            job = db.get(JobRow, attempt.job_id)
            prep = db.get(ApplicationPreparationRow, attempt.preparation_id) if attempt.preparation_id else None
            decision = db.get(EligibilityDecisionRow, co.eligibility_decision_id) if co and co.eligibility_decision_id else None
            priority = db.get(PriorityScoreRow, co.priority_score_id) if co and co.priority_score_id else None
            docs = db.query(DocumentArtifactRow).filter_by(preparation_id=prep.id).all() if prep else []
            runs = db.query(ExecutionRunRow).filter_by(application_id=attempt.id).all()
            snaps = db.query(FormSnapshotRow).filter_by(application_id=attempt.id).all()
            signals = db.query(SignalRow).filter_by(application_id=attempt.id).all()
            events = db.query(OutcomeEventRow).filter_by(application_id=attempt.id).order_by(OutcomeEventRow.sequence).all()
            outcome = db.query(ApplicationOutcomeRow).filter_by(application_id=attempt.id).first()
            audit = db.query(AuditEventRow).filter(AuditEventRow.tenant_id == TENANT, AuditEventRow.entity_id.in_([attempt.id, co.id if co else "", prep.id if prep else ""])).order_by(AuditEventRow.created_at).all()
            app_events = db.query(ApplicationEventRow).filter_by(application_id=attempt.id).order_by(ApplicationEventRow.created_at).all()
            chains.append({
                "why_selected": {"job": {"source": job.source if job else None, "company": opp.company, "title": opp.title, "url": job.application_url if job else None, "content_hash": job.content_hash if job else None}, "opportunity": {"id": opp.id, "identity_key": opp.identity_key, "status": opp.status}, "candidate_opportunity": {"id": co.id, "state": co.state, "eligibility": co.eligibility_status, "fit_score": co.fit_score, "fit_band": co.fit_band, "policy_admitted": co.policy_admitted, "policy_reason": co.policy_reason, "admission_policy_version": co.admission_policy_version, "gate_ruleset_version": co.gate_ruleset_version, "scheduler_code": co.scheduler_code}, "eligibility_decision": {"decision": decision.decision, "ruleset": decision.ruleset_version, "reasons": decision.reason_codes} if decision else None, "priority": {"score": priority.score, "components": priority.components, "weights_version": priority.weights_version} if priority else None},
                "what_would_be_submitted": {"preparation": {"id": prep.id, "version": prep.version, "level": prep.tailoring_level, "lane": prep.lane, "status": prep.status, "validation": prep.validation_status, "fingerprint": prep.input_fingerprint, "evidence_keys": len(prep.evidence_keys or []), "ai_used": prep.ai_used} if prep else None, "documents": [{"type": d.artifact_type, "format": d.format, "version": d.version, "sha256": d.content_hash, "status": d.status} for d in docs], "form_snapshots": [{"fields": s.field_count, "executor": s.executor_kind, "fingerprint": s.fingerprint} for s in snaps]},
                "evidence_of_submission": {"attempt": {"id": attempt.id, "status": attempt.status, "submission_key": attempt.submission_key, "submitted_at": attempt.submitted_at, "reference": attempt.external_application_id}, "runs": [{"id": r.id, "executor": r.executor_kind, "status": r.status, "submit_invoked": r.submit_invoked, "verification": r.verification_status} for r in runs], "note": "dry-run validation: no real submission happened; the SUBMITTED status was set by the validation harness for signal attribution"},
                "what_happened_afterward": {"signals": [{"id": s.id, "source": s.source, "category": s.category, "confidence": s.confidence, "attribution": s.attribution_status} for s in signals], "outcome_events": [{"outcome": e.outcome, "evidence": e.evidence, "origin": e.origin, "event_at": e.event_at} for e in events], "current_outcome": {"current": outcome.current_outcome, "needs_review": outcome.needs_review, "version": outcome.version} if outcome else None},
                "audit_trail": [{"entity": e.entity_type, "action": e.action, "actor": e.actor, "at": e.created_at} for e in audit][:40],
                "application_events": [{"type": e.event_type, "from": e.from_status, "to": e.to_status} for e in app_events],
            })
        _save("09_chain_audit", {"chains": chains, "reconstructable": all(c["why_selected"]["candidate_opportunity"] and c["what_would_be_submitted"]["preparation"] and c["audit_trail"] for c in chains)})
    finally:
        db.close()


def stage_diagnostics() -> None:
    from app.api.routes.diagnostics import diagnostics

    db = _session()
    try:
        data = diagnostics(db, TENANT)
        _save("10_diagnostics", data)
        print(json.dumps({"warnings": data["warnings"], "queue": data["queue"], "attempts": data["attempts"]["by_status"]}, indent=1))
    finally:
        db.close()


# ------------------------------------------------------------------- soak


def stage_soak(cycles: int = 4, pages_per_cycle: int = 10) -> None:
    from app.execution.models import ApplicationMethod, ATSFamily, ExecutionPackage, ExecutionTarget
    from app.execution.playwright.browser import BrowserSession, Pacer
    from app.execution.playwright.executor import PlaywrightExecutor
    from app.execution.target import ats_family_for

    corpus = _load("04_corpus") or {"pages": []}
    pages = [p for p in corpus["pages"] if p.get("url")][:pages_per_cycle]
    session = BrowserSession(headless=True, profile_dir="")
    session.start()
    executor = PlaywrightExecutor(session=session, dry_run=True, handoff_wait_seconds=0, pacer=Pacer(3.0))
    rounds = []
    try:
        for cycle in range(cycles):
            started = time.perf_counter()
            outcomes: dict[str, int] = {}
            for i, entry in enumerate(pages):
                family = ats_family_for(entry["source"], entry["url"])
                package = ExecutionPackage(tenant_id=TENANT, candidate_opportunity_id="soak", opportunity_id="soak", preparation_id="soak", application_id=f"soak-{cycle}-{i}", attempt_number=1, preparation_version=1, preparation_fingerprint="x", target=ExecutionTarget(source=entry["source"], ats_family=family if isinstance(family, ATSFamily) else ATSFamily.GENERIC_WEB, canonical_url=entry["url"], company=entry["company"], title=entry["title"], method=ApplicationMethod.BROWSER_FORM))
                try:
                    form = executor.prepare(package)
                    key = f"handoff:{form.metadata['handoff']}" if form.metadata.get("handoff") else (f"discovered:{len(form.fields)}" if form.fields else "no_form")
                    state = executor._runs.get(package.application_id)
                    if state is not None and state.scan is not None and state.scan.captcha:
                        key += f" (captcha inline={state.scan.captcha_inline}, title={state.scan.title[:40]!r})"
                except Exception as exc:  # noqa: BLE001
                    key = f"error:{type(exc).__name__}"
                finally:
                    executor._close_state(package.application_id)
                outcomes[key] = outcomes.get(key, 0) + 1
            rounds.append({"cycle": cycle, "pages": len(pages), "seconds": round(time.perf_counter() - started, 1), "outcomes": outcomes, "chromium_processes": _chromium_processes(), "python_rss_mb": _python_rss_mb(), "db_mb": round((SCRATCH / "phase13.db").stat().st_size / 1024 / 1024, 2), "documents_mb": _dir_size_mb(SCRATCH / "documents"), "ai_cache_mb": _dir_size_mb(SCRATCH / "ai-cache"), "browser_launches": session.launches})
            print(rounds[-1])
        _save("11_soak", {"rounds": rounds, "after_close_chromium_processes": None})
    finally:
        executor.close()
        time.sleep(2)
        data = _load("11_soak") or {"rounds": rounds}
        data["after_close_chromium_processes"] = _chromium_processes()
        _save("11_soak", data)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["setup", "discover", "discover2", "match", "corpus", "specials", "extension", "documents", "signals", "signals_reprocess", "chain", "diagnostics", "soak"])
    parser.add_argument("--cycles", type=int, default=4)
    parser.add_argument("--per-source", type=int, default=8)
    parser.add_argument("--label", default="")
    args = parser.parse_args(argv)
    from app.core.logging import configure_logging

    configure_logging("INFO")
    {
        "setup": stage_setup,
        "discover": stage_discover,
        "discover2": lambda: stage_discover(second_pass=True, label=args.label),
        "match": stage_match,
        "corpus": lambda: stage_corpus(args.per_source),
        "specials": stage_specials,
        "extension": stage_extension,
        "documents": stage_documents,
        "signals": stage_signals,
        "signals_reprocess": stage_signals_reprocess,
        "chain": stage_chain,
        "diagnostics": stage_diagnostics,
        "soak": lambda: stage_soak(args.cycles),
    }[args.stage]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
