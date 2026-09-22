"""A Python stand-in for the extension's service worker (tests only).

The real extension has three parts: the page scripts (``discover.js``,
``fill.js``, ``content.js``), the service worker (``background.js``) that
talks to the API, and the server. Node is not available here, so the page
scripts are executed verbatim under Playwright against the local fixture
forms, while this driver plays the service worker's part step for step
(claim → start → discover → form → documents → fill → arm → gate → result),
calling the same ``ExecutionService`` methods the HTTP routes call.
"""

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional

from app.execution.executors.extension import BrowserExtensionExecutor
from app.execution.models import (
    ExecutionResult,
    ExecutorKind,
    FieldAnswerStatus,
    FormSnapshot,
    HandoffReason,
)

EXTENSION_SRC = Path(__file__).resolve().parents[2] / "extension" / "src"
PAGE_SCRIPTS = ("discover.js", "fill.js", "content.js")
STUB_BRIDGE = "() => { window.careerosBridge = {send: (m) => window.__careerosBridge(JSON.stringify(m))}; }"


class ExtensionDriver:
    def __init__(self, harness, page, worker: str = "ext-test", mode: str = "manual", wait_ms: int = 3000, version: str = "extension-test"):
        self.harness = harness
        self.service = harness.service
        self.service.executors[ExecutorKind.BROWSER_EXTENSION] = BrowserExtensionExecutor()
        self.page = page
        self.worker = worker
        self.mode = mode
        self.wait_ms = wait_ms
        self.version = version
        self.item = None
        self.attempt = None
        self.run = None
        self.package = None
        self.phase = "idle"
        self.reported: Optional[dict[str, Any]] = None
        self.report_outcome: Optional[dict[str, Any]] = None
        self.gate_calls: list[dict[str, Any]] = []
        self.bridge_log: list[dict[str, Any]] = []
        self.diagnostics: dict[str, Any] = {}
        self.pending: list[dict[str, Any]] = []
        self.artifacts_sent: dict[str, Any] = {}
        page.expose_function("__careerosBridge", self._bridge)

    # ------------------------------------------------------------ bridge
    def _bridge(self, raw: str) -> dict[str, Any]:
        message = json.loads(raw)
        self.bridge_log.append(message)
        if message["type"] == "gate":
            try:
                reply = self.service.gate(self.item, self.worker)
            except Exception as exc:  # noqa: BLE001 - reported to the page like the worker would
                reply = {"ok": False, "failures": [str(exc)]}
            self.gate_calls.append(reply)
            if not reply["ok"]:
                self._report({"outcome": "NEEDS_REVIEW", "submit_attempted": False, "error_class": "POLICY", "message": "pre-submit gate failed: " + "; ".join(reply["failures"][:3]), "stopped_at": "before_submit", "diagnostics": {"gate_failures": reply["failures"][:10]}})
            else:
                self.phase = "submitted"
            return {"ok": reply["ok"], "failures": reply["failures"]}
        if message["type"] == "report":
            return self._report(message["result"])
        return {"ok": False, "error": "unexpected"}

    def _report(self, result: dict[str, Any]) -> dict[str, Any]:
        if self.reported is not None:
            return {"ok": False, "error": "already reported"}
        merged = dict(result)
        merged["diagnostics"] = {**self.diagnostics, **(result.get("diagnostics") or {})}
        self.reported = merged
        self.report_outcome = self.service.report_result(self.item, self.worker, ExecutionResult(**merged))
        self.phase = "handoff" if merged["outcome"] == "HANDOFF" else "settled"
        return {"ok": True}

    # -------------------------------------------------------------- page
    def inject(self) -> None:
        self.page.evaluate(STUB_BRIDGE)
        for name in PAGE_SCRIPTS:
            self.page.add_script_tag(content=(EXTENSION_SRC / name).read_text(encoding="utf-8"))

    def content(self, message: dict[str, Any]) -> dict[str, Any]:
        reply = self.page.evaluate("(m) => window.__careerosContent.handle(m)", message)
        assert reply.get("ok"), reply
        return reply

    # -------------------------------------------------------------- flow
    def fill(self, attempt, navigate: bool = True) -> str:
        """Everything background.runFill does; returns the phase reached."""
        self.attempt = attempt
        if self.item is None:
            item = self.harness.item(attempt)
            assert item is not None
            self.item = self.service.claim_item(item, self.worker)
            assert self.item is not None, "claim refused"
        if self.run is None:
            run, package, outcome = self.service.start(self.item, self.worker, ExecutorKind.BROWSER_EXTENSION)
            if outcome["outcome"] != "started":
                self.phase = "settled"
                self.start_outcome = outcome
                return self.phase
            self.run, self.package = run, package
        if navigate:
            self.page.goto(self.package.target.canonical_url)
        self.inject()
        scan = self.content({"type": "discover"})["scan"]
        self.scan = scan
        handoff = self._handoff_for(scan)
        if handoff:
            reason, message, stopped_at, remaining = handoff
            self.service.handoff(self.item, self.worker, HandoffReason(reason), message, stopped_at, remaining)
            self.phase = "handoff"
            self.handoff_reason = reason
            return self.phase
        form = FormSnapshot(source_url=scan["url"], executor_kind=ExecutorKind.BROWSER_EXTENSION, executor_version=self.version, fields=[self._field(f) for f in scan["fields"]], metadata={"title": scan.get("title", "")[:200], "custom_widgets": scan.get("custom_widgets", 0)})
        snapshot, answers = self.service.capture_form(self.attempt, form, self.run, self.package)
        self.service.db.commit()
        joined = []
        for field, answer in zip(scan["fields"], answers):
            joined.append({"field_id": answer.field_id, "label": answer.label, "selector": field["selector"], "field_type": field["field_type"], "required": answer.required, "status": answer.status.value, "source": answer.source.value, "answer": answer.answer, "selected_values": list(answer.selected_values), "artifact_type": answer.artifact_type, "reason": answer.reason})
        self.answers = joined
        artifacts, problems = self._artifacts(joined)
        self.artifacts_sent = artifacts
        report = self.content({"type": "fill", "answers": joined, "artifacts": artifacts})["report"]
        self.fill_report = report
        self.diagnostics = {"fields_filled": len(report["filled"]), "fields_skipped": len(report["skipped"]), "missing_required": report["missing_required"][:10], "uploads": [{"type": k, "artifact_id": v["artifact_id"], "version": v["version"], "sha256": v["sha256"]} for k, v in artifacts.items()], "document_problems": problems[:5]}
        missing_files = report["file_missing"] + problems
        if missing_files:
            self.service.handoff(self.item, self.worker, HandoffReason.ARTIFACT_FILE_REQUIRED, "a required upload has no usable rendered document: " + "; ".join(missing_files[:2]), "fill", ["attach the document yourself", "submit and confirm"])
            self.phase = "handoff"
            self.handoff_reason = "ARTIFACT_FILE_REQUIRED"
            return self.phase
        self.submit_locators = [b["selector"] for b in scan.get("submit_buttons", [])]
        if self.mode == "dry_run":
            self.content({"type": "arm", "mode": "dry_run", "locators": self.submit_locators})
            self._report({"outcome": "DRY_RUN", "submit_attempted": False, "message": "dry run: filled in the browser, submit deliberately not pressed", "stopped_at": "before_submit"})
            return self.phase
        self.content({"type": "arm", "mode": self.mode, "locators": self.submit_locators, "wait_ms": self.wait_ms})
        self.pending = [a for a in joined if a["status"] in (FieldAnswerStatus.NEEDS_USER_INPUT.value, FieldAnswerStatus.NEEDS_REVIEW.value)]
        if self.pending or report["missing_required"]:
            self.phase = "awaiting_user"
            return self.phase
        self.phase = "filled"
        if self.mode == "auto":
            self.submit()
        return self.phase

    def answer(self, field_id: str, answer: str, save_to_bank: bool = False) -> str:
        self.service.answer_field(field_id, answer, "user", save_to_bank)
        self.service.db.commit()
        return self.fill(self.attempt, navigate=False)

    def submit(self) -> None:
        """The extension presses submit (auto mode / "Submit now")."""
        self.content({"type": "submit", "locators": self.submit_locators})

    def user_clicks(self, selector: str) -> None:
        """The person presses the site's own submit button."""
        self.page.click(selector)

    def wait(self, timeout_s: float = 8.0) -> dict[str, Any]:
        """What the service worker does after the click: wait for the page's
        report, re-inject after a navigation, give up as UNKNOWN."""
        url_before = self.page.url
        deadline = time.monotonic() + timeout_s
        reinjected = False
        while self.reported is None and time.monotonic() < deadline:
            self.page.wait_for_timeout(200)
            if self.phase == "submitted" and not reinjected and self.page.url != url_before:
                reinjected = True
                self.page.wait_for_load_state()
                self.inject()
                result = self.content({"type": "classify", "url_before": url_before}).get("result")
                if result:
                    self._report(result)
                else:
                    self.content({"type": "observe", "url_before": url_before, "wait_ms": self.wait_ms})
        if self.reported is None and self.phase == "submitted":
            self._report({"outcome": "UNKNOWN", "submit_attempted": True, "error_class": "TIMEOUT", "message": "no result observed within the window; outcome unknown", "stopped_at": "after_submit"})
        return self.report_outcome or {}

    # ----------------------------------------------------------- helpers
    @staticmethod
    def _handoff_for(scan):
        if scan.get("captcha") and scan.get("captcha_visible") and not scan.get("captcha_solved"):
            return ("CAPTCHA_REQUIRED", "a CAPTCHA / human-verification challenge is on the page", "captcha", ["complete the human verification yourself", "press Fill again"])
        if scan.get("mfa"):
            return ("MFA_REQUIRED", "a one-time code prompt is on the page", "mfa", ["enter the code yourself"])
        if scan.get("login_wall"):
            return ("AUTH_REQUIRED", "the page asks for a login", "login", ["sign in yourself"])
        if not scan.get("fields"):
            return ("UNSUPPORTED_FORM", "no fillable form controls were found on this page", "discover", ["open the application form"])
        return None

    @staticmethod
    def _field(f):
        return {"external_id": f.get("external_id"), "label": f.get("label") or "", "field_type": f["field_type"], "required": bool(f.get("required")), "options": [{"label": o.get("label") or "", "value": None if o.get("value") is None else str(o["value"])} for o in f.get("options") or []], "current_value": None if f.get("current_value") is None else str(f["current_value"])[:1024], "accept": f.get("accept"), "selector": f.get("selector"), "input_type": f.get("input_type")}

    def _artifacts(self, answers):
        wanted = {a["artifact_type"] for a in answers if a["status"] == "ANSWERED" and a["field_type"] == "file" and a["artifact_type"]}
        declared = self.package.execution_config.get("artifacts", {})
        out, problems = {}, []
        for artifact_type in sorted(wanted):
            meta = declared.get(artifact_type)
            if not meta:
                problems.append(f"{artifact_type}: no rendered document for this preparation")
                continue
            try:
                _, path = self.service.documents.materialize(meta["id"], for_upload=True)
                data = Path(path).read_bytes()
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{artifact_type}: {exc}")
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest != meta["sha256"]:
                problems.append(f"{artifact_type}: document bytes do not match the recorded SHA-256; not uploaded")
                continue
            if not data:
                problems.append(f"{artifact_type}: empty document; not uploaded")
                continue
            out[artifact_type] = {"base64": base64.b64encode(data).decode("ascii"), "name": Path(path).name, "mime": "application/pdf", "sha256": digest, "artifact_id": meta["id"], "version": meta["version"]}
        return out, problems
