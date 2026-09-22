"""The local Playwright executor: the first real :class:`Executor`.

    prepare(package)  -> open the page, stop on CAPTCHA/login/MFA, discover the form
    execute(...)      -> fill only mapped, safe answers; upload only real local
                         files; re-run the pre-submit gate; press submit (never in
                         dry-run); classify what happened; never assume success
    verify(...)       -> confirmation URL / text / reference seen after submit

Everything runs in the candidate's own browser process. There is no
CAPTCHA solving, fingerprint spoofing, proxy rotation or rate-limit evasion:
those situations end in a human handoff with exact instructions.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from app.career.repository import normalize_question
from app.config import settings
from app.core.timeutils import utc_now
from app.execution.executors.base import Executor, ExecutorError
from app.execution.forms import choose_option, search_terms
from app.execution.models import (
    ErrorClass,
    ExecutionOutcome,
    ExecutionPackage,
    ExecutionResult,
    ExecutionTarget,
    ExecutorKind,
    FieldAnswer,
    FieldAnswerStatus,
    FieldType,
    FormField,
    FormSnapshot,
    HandoffReason,
    VerificationMethod,
    VerificationResult,
    VerificationStatus,
)
from app.execution.playwright.browser import BrowserSession, BrowserUnavailable, Pacer
from app.execution.playwright.discovery import PageScan, scan_page
from app.execution.playwright.strategies import Strategy, strategy_for

logger = logging.getLogger(__name__)

EXECUTOR_VERSION = "playwright-local-v1"
_CRASH_RE = re.compile(r"target (page|context|browser) has been closed|target crashed|page crashed|browser has been closed|connection closed|has been closed", re.IGNORECASE)
_TIMEOUT_RE = re.compile(r"timeout", re.IGNORECASE)
_NET_RE = re.compile(r"net::ERR_|ERR_NAME_NOT_RESOLVED|ERR_CONNECTION|ERR_INTERNET_DISCONNECTED", re.IGNORECASE)
_SUBMIT_TEXT_RE = re.compile(r"submit|apply|send|continue|next", re.IGNORECASE)
_CHALLENGE_FRAME_RE = re.compile(r"recaptcha|hcaptcha|turnstile|captcha", re.IGNORECASE)


_APPLICANT_FIELD_RE = re.compile(r"name|email|phone|resume|cv|cover|linkedin|portfolio|website|address|location|pronoun|salary|start|authori|sponsor|applicant|candidate|job_application|_systemfield_", re.IGNORECASE)


def _looks_like_submit(button: dict[str, Any]) -> bool:
    return button.get("type") == "submit" or bool(_SUBMIT_TEXT_RE.search(button.get("text", "") or ""))


def _looks_like_application_form(scan: PageScan) -> bool:
    """A search box, newsletter signup or cookie form is not an application.

    An application form has an upload, an e-mail, or at least one control
    whose name or label is about the applicant. Board index pages that a
    closed posting redirects to (Phase 13: Greenhouse) fail this on purpose.
    """
    for f in scan.fields:
        if f.field_type in (FieldType.FILE, FieldType.EMAIL, FieldType.PHONE):
            return True
        if _APPLICANT_FIELD_RE.search(f"{f.external_id or ''} {f.label or ''}"):
            return True
    return False

#: Pre-submit gate callback: returns human-readable failures (empty = go).
Gate = Callable[[], list[str]]


@dataclass
class RunState:
    """Per-package state kept between prepare() and execute()."""

    page: Any = None
    page_cm: Any = None
    #: Where the form lives: the page's main frame, or a child frame when an
    #: ATS is embedded in the employer's site (Greenhouse embeds, Phase 13).
    target: Any = None
    strategy: Optional[Strategy] = None
    scan: Optional[PageScan] = None
    handoff: Optional[tuple[HandoffReason, str]] = None
    steps: list[str] = field(default_factory=list)
    post_submit: Optional[PageScan] = None
    post_submit_url: Optional[str] = None
    started: float = field(default_factory=time.monotonic)
    dry_run: bool = True
    settle_ms: int = 0

    def step(self, name: str) -> None:
        self.steps.append(name)


class PlaywrightExecutor(Executor):
    kind = ExecutorKind.PLAYWRIGHT_LOCAL
    version = EXECUTOR_VERSION

    def __init__(
        self,
        session: Optional[BrowserSession] = None,
        dry_run: Optional[bool] = None,
        submit_wait_ms: Optional[int] = None,
        handoff_wait_seconds: Optional[int] = None,
        pacer: Optional[Pacer] = None,
        artifact_files: Optional[dict[str, str]] = None,
        debug_artifacts_dir: Optional[str] = None,
        settle_ms: Optional[int] = None,
    ):
        self.session = session or BrowserSession()
        self.dry_run = settings.playwright_dry_run if dry_run is None else dry_run
        self.submit_wait_ms = submit_wait_ms or settings.playwright_submit_wait_ms
        self.settle_ms = settings.playwright_settle_ms if settle_ms is None else settle_ms
        self.handoff_wait_seconds = settings.playwright_handoff_wait_seconds if handoff_wait_seconds is None else handoff_wait_seconds
        self.pacer = pacer or Pacer()
        self.artifact_files = dict(artifact_files or {})
        self.debug_artifacts_dir = debug_artifacts_dir if debug_artifacts_dir is not None else settings.playwright_debug_artifacts_dir
        self._runs: dict[str, RunState] = {}
        self.submit_clicks = 0
        #: Why a control was left alone during the current fill, by field label.
        self.fill_notes: dict[str, str] = {}

    # ---------------------------------------------------------------- contract

    def can_handle(self, target: ExecutionTarget) -> bool:
        url = (target.canonical_url or "").lower()
        return target.method.value == "browser_form" and (url.startswith("http://") or url.startswith("https://") or url.startswith("file://"))

    def prepare(self, package: ExecutionPackage) -> FormSnapshot:
        state = self._open(package)
        scan = state.scan
        strategy = state.strategy
        fields = list(scan.fields)
        for f in fields:
            if f.external_id and f.external_id in strategy.field_labels and (not f.label or f.label == f.external_id):
                f.label = strategy.field_labels[f.external_id]
        metadata = {
            "executor": self.kind.value,
            "strategy": strategy.name,
            "page_title": scan.title[:200],
            "custom_widgets": scan.custom_widgets,
            "submit_buttons": [b.get("text", "")[:60] for b in scan.submit_buttons[:5]],
            "handoff": state.handoff[0].value if state.handoff else None,
            "dry_run": self.dry_run,
            "in_frame": state.target is not None and state.target is not state.page,
            "settle_ms": state.settle_ms,
        }
        return FormSnapshot(source_url=scan.url or package.target.canonical_url, fields=fields, executor_kind=self.kind, executor_version=self.version, metadata=metadata)

    def execute(self, package: ExecutionPackage, form: FormSnapshot, answers: list[FieldAnswer], gate: Optional[Gate] = None) -> ExecutionResult:
        state = self._runs.get(package.application_id)
        if state is None or state.page is None:
            state = self._open(package)
        try:
            return self._execute(package, form, answers, gate, state)
        finally:
            self._close_state(package.application_id)

    def verify(self, package: ExecutionPackage, result: ExecutionResult) -> VerificationResult:
        """Only what the browser actually showed after submit counts."""
        post = self._post_submit_evidence(package)
        if post is None:
            if result.confirmation_reference or result.external_application_id:
                return VerificationResult(status=VerificationStatus.LIKELY, method=VerificationMethod.EXECUTOR_REPORT, detail="reference reported by the executor; no page evidence retained", confirmation_reference=result.confirmation_reference, external_application_id=result.external_application_id)
            return VerificationResult(status=VerificationStatus.UNKNOWN, method=VerificationMethod.NONE, detail="no post-submit page evidence")
        strategy, scan, url = post
        reference = scan.reference
        if reference:
            return VerificationResult(status=VerificationStatus.VERIFIED, method=VerificationMethod.APPLICATION_ID, detail=f"reference on confirmation page: {reference}", confirmation_reference=reference, external_application_id=reference, application_url=url)
        marker = scan.success_marker or (strategy.success_text.search(scan.body_excerpt).group(0) if strategy.success_text and strategy.success_text.search(scan.body_excerpt) else None)
        if marker:
            return VerificationResult(status=VerificationStatus.VERIFIED, method=VerificationMethod.CONFIRMATION_TEXT, detail=f"confirmation text: {marker[:80]}", application_url=url)
        if any(p in (url or "").lower() for p in strategy.success_url_patterns):
            return VerificationResult(status=VerificationStatus.LIKELY, method=VerificationMethod.REDIRECT_URL, detail="redirected to a confirmation-looking URL without confirmation text", application_url=url)
        return VerificationResult(status=VerificationStatus.UNKNOWN, method=VerificationMethod.NONE, detail="no confirmation evidence on the page after submit")

    # ---------------------------------------------------------------- opening

    def _open(self, package: ExecutionPackage) -> RunState:
        self._close_state(package.application_id)
        state = RunState(dry_run=self.dry_run)
        state.strategy = strategy_for(package.target)
        self._runs[package.application_id] = state
        try:
            self.session.start()
        except BrowserUnavailable as exc:
            raise ExecutorError(str(exc), before_submit=True, retryable=False, error_class=ErrorClass.EXECUTOR_CRASH) from exc
        state.page_cm = self.session.page()
        try:
            state.page = state.page_cm.__enter__()
        except Exception as exc:  # noqa: BLE001 - browser died between executions
            self._relaunch_after(exc)
            raise ExecutorError(f"browser unavailable: {self._brief(exc)}", before_submit=True, retryable=True, error_class=ErrorClass.BROWSER_CRASH) from exc
        state.step("navigate")
        self.pacer.wait()
        try:
            state.page.goto(package.target.canonical_url, wait_until="domcontentloaded")
            state.page.wait_for_load_state("load", timeout=self.session.navigation_timeout_ms)
        except Exception as exc:  # noqa: BLE001 - classified below
            self._close_state(package.application_id)
            raise self._classify_navigation(exc) from exc
        state.step("scan")
        try:
            state.target, state.scan = self._settled_scan(state)
        except Exception as exc:  # noqa: BLE001
            self._close_state(package.application_id)
            raise ExecutorError(f"form discovery failed: {self._brief(exc)}", before_submit=True, retryable=True, error_class=ErrorClass.EXECUTOR_CRASH) from exc
        state.handoff = self._handoff_condition(state.scan, state.strategy)
        if state.handoff is not None and state.handoff[0] is HandoffReason.AMBIGUOUS_FORM and state.scan.apply_links:
            # A listing page in front of the form (Stripe, employer sites in front of
            # Greenhouse, 2026-09-22): follow its own "Apply" link once and scan again.
            if self._follow_apply_link(state):
                state.handoff = self._handoff_condition(state.scan, state.strategy)
        return state

    def _follow_apply_link(self, state: RunState) -> bool:
        """Open the page's "Apply" link / button in the same tab and re-scan.

        Navigation only: nothing is typed and nothing is submitted. False when
        the link cannot be followed or no application form appears after it.
        """
        link = state.scan.apply_links[0]
        page = state.page
        state.step("follow_apply")
        try:
            href = (link.get("href") or "").strip()
            if href and not href.startswith("#") and not href.lower().startswith("javascript:"):
                target_url = page.evaluate("(h) => new URL(h, location.href).href", href)
                page.goto(target_url, wait_until="domcontentloaded")
            else:
                locator = page.locator(link["selector"]).first
                if not locator.count():
                    return False
                locator.click(timeout=self.session.action_timeout_ms)
            page.wait_for_load_state("load", timeout=self.session.navigation_timeout_ms)
            state.target, state.scan = self._settled_scan(state)
        except Exception as exc:  # noqa: BLE001 - the listing page is then handed off as before
            logger.info("Apply link %r could not be followed: %s", link.get("text", ""), self._brief(exc))
            return False
        return bool(state.scan.fields and _looks_like_application_form(state.scan))

    def _settled_scan(self, state: RunState):
        """Scan the main frame, then any embedded ATS frame, re-scanning for
        up to ``settle_ms`` while a client-side form is still rendering.

        Returns ``(target, scan)`` where ``target`` is the page or the frame
        the form was found in. Stops early on a form, a challenge, or a login
        wall; a page that never shows a form is handed off by the caller.
        """
        page = state.page
        deadline = time.monotonic() + max(0, self.settle_ms) / 1000.0
        started = time.monotonic()
        best = (page, scan_page(page))
        while True:
            target, scan = best
            if (scan.fields and _looks_like_application_form(scan)) or scan.captcha_visible or scan.mfa or scan.login_wall:
                break
            for frame in page.frames:
                if frame is page.main_frame or _CHALLENGE_FRAME_RE.search(frame.url or ""):
                    continue
                try:
                    frame_scan = scan_page(frame)
                except Exception:  # noqa: BLE001 - detached or navigating frame; try the next
                    continue
                if len(frame_scan.fields) > len(best[1].fields):
                    best = (frame, frame_scan)
            if (best[1].fields and _looks_like_application_form(best[1])) or time.monotonic() >= deadline:
                break
            page.wait_for_timeout(500)
            best = (page, scan_page(page))
        state.settle_ms = int((time.monotonic() - started) * 1000)
        return best

    def _handoff_condition(self, scan: PageScan, strategy: Strategy) -> Optional[tuple[HandoffReason, str]]:
        if scan.captcha and scan.captcha_visible and not scan.captcha_solved:
            if scan.captcha_inline:
                return HandoffReason.CAPTCHA_REQUIRED, "the application form includes a human verification widget; complete it in the browser window and the run continues (nothing is typed before that)"
            return HandoffReason.CAPTCHA_REQUIRED, "the page shows a CAPTCHA / human verification challenge"
        if scan.mfa:
            return HandoffReason.MFA_REQUIRED, "the page asks for a one-time / two-factor code"
        if scan.login_wall and not any(f.field_type is FieldType.FILE for f in scan.fields):
            return HandoffReason.AUTH_REQUIRED, "the page asks for a login before the application form"
        if strategy.strict_controls and scan.custom_widgets and len(scan.fields) < 2:
            return HandoffReason.UNSUPPORTED_FORM, f"UNSUPPORTED_FORM: {scan.custom_widgets} custom widget(s) and no standard controls ({strategy.name})"
        if not scan.fields:
            # Cookie banners and menus have buttons too; they are not a form. Nor is a
            # page whose only control is a search box's button (2026-09-22): with
            # nothing to fill there is nothing that may be submitted.
            return HandoffReason.AMBIGUOUS_FORM, "no application form found on the page"
        if scan.fields and not _looks_like_application_form(scan):
            return HandoffReason.AMBIGUOUS_FORM, "the page has a form, but not an application form (no contact, upload or applicant fields; a search or listing page?)"
        return None

    # -------------------------------------------------------------- executing

    def _execute(self, package: ExecutionPackage, form: FormSnapshot, answers: list[FieldAnswer], gate: Optional[Gate], state: RunState) -> ExecutionResult:
        page, scan, strategy = state.target or state.page, state.scan, state.strategy
        diagnostics: dict[str, Any] = {"strategy": strategy.name, "url": scan.url, "dry_run": self.dry_run, "fields_discovered": len(scan.fields), "in_frame": page is not state.page, "settle_ms": state.settle_ms}
        if scan.captcha and not scan.captcha_visible:
            diagnostics["captcha_invisible"] = True  # the provider scores the submit; a challenge after the click hands off
        if state.handoff is not None:
            reason, message = state.handoff
            cleared = self._wait_for_person(state, reason)
            if not cleared:
                return self._handoff_result(reason, message, state, diagnostics, stopped_at="before_form")
            # Audit 2026-09-14: a wall page (no form) was mapped to zero answers; once the
            # person cleared it the real form appeared and was submitted unfilled.
            self._require_mapped_form(form, state)
            page, scan = state.target or state.page, state.scan
        by_key = {f.external_id: f for f in form.fields if f.external_id}
        by_label = {f.label: f for f in form.fields}
        filled: list[str] = []
        skipped: list[str] = []
        missing_required: list[str] = []
        file_missing: list[str] = []
        #: Required and answered, but the control could not be set (unknown type,
        #: a searchable dropdown or yes / no buttons offering no option that names
        #: the answer): submitting would leave it empty.
        unfillable_required: list[str] = []
        notes = self.fill_notes = {}
        state.step("fill")
        for answer in answers:
            target = by_key.get(answer.external_id or "") or by_label.get(answer.label)
            if target is None or not target.selector:
                if answer.required and answer.status is FieldAnswerStatus.ANSWERED:
                    missing_required.append(answer.label)
                continue
            if answer.status is not FieldAnswerStatus.ANSWERED:
                if answer.required:
                    missing_required.append(answer.label)
                else:
                    skipped.append(answer.label)
                continue
            try:
                outcome = self._fill(page, target, answer, package)
            except ExecutorError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise self._classify_fill(exc) from exc
            if outcome == "filled":
                filled.append(answer.label)
                if target.field_type is FieldType.FILE:
                    uploads = diagnostics.setdefault("uploads", [])
                    uploads.append({"field": answer.label[:80], "artifact": answer.artifact_type, "file": Path(self._artifact_path(answer.artifact_type, package) or "").name})
            elif outcome == "file_missing":
                (file_missing if answer.required else skipped).append(answer.label)
            else:
                skipped.append(answer.label)
                if answer.required:
                    unfillable_required.append(answer.label)
        diagnostics.update({"fields_filled": len(filled), "fields_skipped": len(skipped), "missing_required": missing_required[:20], "filled": filled[:40]})
        if notes:
            diagnostics["unfilled_notes"] = {k[:80]: v[:160] for k, v in list(notes.items())[:20]}
        if missing_required:
            return ExecutionResult(outcome=ExecutionOutcome.NEEDS_USER_INPUT, message=f"{len(missing_required)} required field(s) have no safe answer: " + ", ".join(missing_required[:5]), stopped_at="fill", diagnostics=diagnostics)
        if file_missing:
            return self._handoff_result(HandoffReason.ARTIFACT_FILE_REQUIRED, "a required upload has no intact local document: " + ", ".join(file_missing[:3]) + " (render the preparation's documents, or set EXECUTION_RESUME_FILE / EXECUTION_COVER_LETTER_FILE, or upload by hand)", state, diagnostics, stopped_at="upload", remaining=["provide the document file", "retry"])
        if unfillable_required:
            diagnostics["unfillable_required"] = unfillable_required[:20]
            detail = "; ".join(f"{label}: {notes[label]}" for label in unfillable_required[:3] if label in notes)
            return self._handoff_result(HandoffReason.UNSUPPORTED_FORM, "required field(s) could not be set on the page: " + ", ".join(unfillable_required[:3]) + (f" ({detail})" if detail else ""), state, diagnostics, stopped_at="fill", remaining=["answer those questions on the employer page and submit by hand", "confirm the outcome here"])
        # Late challenge (some sites show it after filling)?
        state.step("rescan")
        try:
            rescan = scan_page(page)
        except Exception as exc:  # noqa: BLE001
            raise self._classify_fill(exc) from exc
        late = self._handoff_condition(rescan, strategy)
        if late is not None and late[0] in (HandoffReason.CAPTCHA_REQUIRED, HandoffReason.MFA_REQUIRED, HandoffReason.AUTH_REQUIRED):
            state.handoff = late
            if not self._wait_for_person(state, late[0]):
                return self._handoff_result(late[0], late[1], state, diagnostics, stopped_at="before_submit")
            self._require_mapped_form(form, state)
            page, rescan = state.target or state.page, state.scan
        submit =self._find_submit(page, strategy, rescan)
        if submit is None:
            return self._handoff_result(HandoffReason.AMBIGUOUS_FORM, "no submit control could be identified", state, diagnostics, stopped_at="before_submit", remaining=["submit the form by hand"])
        if self.dry_run:
            diagnostics["would_submit_with"] = submit[1]
            return ExecutionResult(outcome=ExecutionOutcome.DRY_RUN, submit_attempted=False, message=f"dry run: {len(filled)} field(s) filled, {len(skipped)} skipped, submit not pressed", stopped_at="before_submit", diagnostics=diagnostics)
        if gate is not None:
            state.step("gate")
            failures = gate()
            if failures:
                diagnostics["gate_failures"] = failures[:10]
                return ExecutionResult(outcome=ExecutionOutcome.NEEDS_REVIEW, submit_attempted=False, error_class=ErrorClass.POLICY, message="pre-submit gate failed: " + "; ".join(failures[:3]), stopped_at="before_submit", diagnostics=diagnostics)
        # ---- the click ----------------------------------------------------
        state.step("submit")
        url_before = page.url
        try:
            self.submit_clicks += 1
            submit[0].click(timeout=self.session.action_timeout_ms)
        except Exception as exc:  # noqa: BLE001 - after this point nothing is "not submitted"
            return self._unknown(f"submit click raised: {self._brief(exc)}", diagnostics, exc)
        return self._after_submit(page, state, strategy, url_before, diagnostics)

    def _after_submit(self, page, state: RunState, strategy: Strategy, url_before: str, diagnostics: dict[str, Any]) -> ExecutionResult:
        state.step("wait_result")
        deadline = time.monotonic() + self.submit_wait_ms / 1000.0
        last_scan: Optional[PageScan] = None
        while True:
            try:
                page.wait_for_timeout(250)
                current = scan_page(page)
            except Exception as exc:  # noqa: BLE001 - page gone after the click: unknown
                if page is not state.page and state.page is not None:
                    # the embedded frame went away (the host page navigated); keep watching the page itself
                    page = state.page
                    continue
                return self._unknown(f"page unavailable after submit: {self._brief(exc)}", diagnostics, exc)
            last_scan = current
            url = current.url
            success_text = current.success_marker or (bool(strategy.success_text and strategy.success_text.search(current.body_excerpt)))
            success_url = url != url_before and any(p in url.lower() for p in strategy.success_url_patterns)
            if success_text or success_url:
                state.post_submit, state.post_submit_url = current, url
                diagnostics["result_url"] = url[:300]
                return ExecutionResult(
                    outcome=ExecutionOutcome.SUBMITTED,
                    submit_attempted=True,
                    application_url=url,
                    external_application_id=current.reference,
                    confirmation_reference=current.reference,
                    message="confirmation " + ("text" if success_text else "URL") + " observed after submit",
                    diagnostics=diagnostics,
                )
            if current.captcha_visible:
                return self._handoff_result(HandoffReason.CAPTCHA_REQUIRED, "a CAPTCHA appeared after pressing submit", state, diagnostics, stopped_at="after_submit", submit_attempted=True, remaining=["complete the challenge", "confirm whether the application went through"])
            if current.validation_errors and url == url_before:
                diagnostics["validation_errors"] = current.validation_errors[:5]
                return ExecutionResult(outcome=ExecutionOutcome.NEEDS_REVIEW, submit_attempted=True, error_class=ErrorClass.VALIDATION, message="the form rejected the submission: " + "; ".join(current.validation_errors[:3]), stopped_at="after_submit", diagnostics=diagnostics)
            if time.monotonic() > deadline:
                break
        state.post_submit, state.post_submit_url = last_scan, last_scan.url if last_scan else None
        diagnostics["result_url"] = (last_scan.url if last_scan else "")[:300]
        return ExecutionResult(outcome=ExecutionOutcome.UNKNOWN, submit_attempted=True, error_class=ErrorClass.TIMEOUT, message="no confirmation, error or redirect observed after submit; outcome unknown", stopped_at="after_submit", diagnostics=diagnostics)

    # ---------------------------------------------------------------- filling

    def _fill(self, page, field: FormField, answer: FieldAnswer, package: Optional[ExecutionPackage] = None) -> str:
        """Set one control; "filled", "skipped" (with a note in ``self.fill_notes``) or "file_missing"."""
        kind = field.field_type
        locator = page.locator(field.selector).first
        notes = self.fill_notes
        if kind is FieldType.COMBOBOX:
            wanted = answer.selected_values[0] if answer.selected_values else answer.answer
            if not wanted:
                return "skipped"
            return self._pick_combobox(page, locator, field, str(wanted), answer.category, notes)
        if kind is FieldType.YESNO:
            if not answer.selected_values:
                return "skipped"
            return self._press_yesno(page, locator, field, answer.selected_values[0], notes)
        if kind is FieldType.FILE:
            path = self._artifact_path(answer.artifact_type, package)
            if path is None:
                return "file_missing"
            page.locator(field.selector).first.set_input_files(path)
            return "filled"
        if kind in (FieldType.TEXT, FieldType.TEXTAREA, FieldType.EMAIL, FieldType.PHONE, FieldType.NUMERIC, FieldType.DATE):
            if answer.answer is None:
                return "skipped"
            locator.fill(str(answer.answer))
            return "filled"
        if kind is FieldType.SELECT:
            if not answer.selected_values:
                return "skipped"
            locator.select_option(answer.selected_values[0])
            return "filled"
        if kind is FieldType.MULTI_SELECT:
            if not answer.selected_values:
                return "skipped"
            if field.input_type == "checkbox":
                for value in answer.selected_values:
                    page.locator(f'{field.selector}[value="{value}"]').first.check()
            else:
                locator.select_option(answer.selected_values)
            return "filled"
        if kind is FieldType.RADIO:
            if not answer.selected_values:
                return "skipped"
            page.locator(f'{field.selector}[value="{answer.selected_values[0]}"]').first.check()
            return "filled"
        if kind is FieldType.CHECKBOX:
            if not answer.selected_values:
                return "skipped"
            locator.check()
            return "filled"
        notes[field.label] = "control type not operated"
        return "skipped"

    #: Where a searchable dropdown renders its choices (react-select, Ashby, ARIA comboboxes).
    _OPTION_SELECTOR = '[role="option"]'
    #: react-select's "No options" / "Loading..." notice.
    _MENU_NOTICE_SELECTOR = '[class*="menu-notice"], [class*="noOptions"], [class*="no-options"]'

    def _pick_combobox(self, page, locator, field: FormField, wanted: str, category: str, notes: dict[str, str]) -> str:
        """Type the answer, wait for the rendered options, pick the one that names it, confirm it shows.

        The same matching rules as the offline mapping (:func:`choose_option`)
        decide; two candidates or none leave the field empty and the run stops
        before submit. The search terms come from :func:`search_terms` (the
        answer, then the city of a "City, State, Country", the bare yes / no
        of a sentence, the level of a degree).
        """
        seen: list[str] = []
        for typed in search_terms(wanted, category):
            locator.click(timeout=self.session.action_timeout_ms)
            locator.fill("")
            press = getattr(locator, "press_sequentially", None) or locator.type
            press(typed, delay=25)
            options = self._wait_for_options(page)
            if not options:
                continue
            labels = [label for label, _ in options]
            seen = labels
            chosen = choose_option(labels, wanted, category=category)
            if chosen is None:
                continue
            options[labels.index(chosen)][1].click(timeout=self.session.action_timeout_ms)
            page.wait_for_timeout(150)
            if self._combobox_shows(locator, chosen):
                return "filled"
            notes[field.label] = f"chose {chosen!r} but the field does not show it"
            return "skipped"
        try:
            locator.fill("")
            locator.press("Escape")
        except Exception:  # noqa: BLE001 - leaving the field empty is the point
            logger.debug("Combobox reset failed", exc_info=True)
        notes[field.label] = f"no option named {wanted!r}" + (f" among {len(seen)} shown" if seen else "; the list showed nothing")
        return "skipped"

    def _wait_for_options(self, page, wait_ms: int = 4000) -> list[tuple[str, Any]]:
        """The visible options a dropdown rendered after typing, once the list settles (or [] in time)."""
        deadline = time.monotonic() + wait_ms / 1000.0
        last: list[str] = []
        stable = 0
        while time.monotonic() < deadline:
            page.wait_for_timeout(150)
            found = page.locator(self._OPTION_SELECTOR)
            texts = []
            handles = []
            for i in range(min(found.count(), 60)):
                option = found.nth(i)
                try:
                    if not option.is_visible():
                        continue
                    text = " ".join(option.inner_text().split())
                except Exception:  # noqa: BLE001 - the list re-rendered under us; scan again
                    texts = []
                    break
                if text and not re.match(r"^(loading|no (options|results)|type to search)", text, re.IGNORECASE):
                    texts.append(text)
                    handles.append(option)
            if texts:
                if texts == last:
                    stable += 1
                    if stable >= 1:
                        return list(zip(texts, handles))
                else:
                    stable = 0
                last = texts
                continue
            notice = page.locator(self._MENU_NOTICE_SELECTOR)
            try:
                if notice.count() and notice.first.is_visible() and re.search(r"no (options|results)", notice.first.inner_text(), re.IGNORECASE):
                    return []
            except Exception:  # noqa: BLE001
                pass
        return []

    @staticmethod
    def _combobox_shows(locator, chosen: str) -> bool:
        """After the click, the control itself must display the choice (never assumed)."""
        shown = locator.evaluate(
            "el => { const box = el.closest('[class*=\"control\"]') || el.closest('[class*=\"container\"]') || el.parentElement;"
            " const v = box && box.querySelector('[class*=\"single-value\"], [class*=\"singleValue\"], [class*=\"selected-value\"], [class*=\"selectedValue\"]');"
            " return [(v ? v.textContent : ''), el.value || '', (box ? box.textContent : '')].join(' | '); }"
        )
        return normalize_question(chosen) in normalize_question(shown or "")

    def _press_yesno(self, page, locator, field: FormField, wanted: str, notes: dict[str, str]) -> str:
        """Press the button, next to the hidden checkbox, whose text is the answer; confirm it reads pressed."""
        buttons = locator.locator("xpath=..").locator("button[aria-pressed]")
        target = None
        for i in range(buttons.count()):
            button = buttons.nth(i)
            if normalize_question(button.inner_text()) == normalize_question(wanted):
                target = button
                break
        if target is None:
            notes[field.label] = f"no button reads {wanted!r}"
            return "skipped"
        if target.get_attribute("aria-pressed") != "true":
            target.click(timeout=self.session.action_timeout_ms)
            page.wait_for_timeout(100)
        if target.get_attribute("aria-pressed") == "true":
            return "filled"
        notes[field.label] = f"pressed {wanted!r} but the button did not take"
        return "skipped"

    def _artifact_path(self, artifact_type: Optional[str], package: Optional[ExecutionPackage] = None) -> Optional[str]:
        """Only real, existing local documents are ever uploaded.

        Preference: the preparation's rendered artifact (Phase 8), whose
        SHA-256 is re-checked here, immediately before the upload; then a
        file injected by the caller; then the configured personal documents.
        """
        if not artifact_type:
            return None
        if package is not None:
            rendered = (package.execution_config.get("artifact_files") or {}).get(artifact_type)
            expected = (package.execution_config.get("artifacts") or {}).get(artifact_type) or {}
            if rendered:
                path = Path(rendered)
                if not path.is_file() or path.stat().st_size == 0:
                    logger.warning("Rendered %s missing on disk; not uploading", artifact_type)
                    return None
                if expected.get("sha256"):
                    from app.documents.storage import sha256_file

                    if sha256_file(path) != expected["sha256"]:
                        logger.warning("Rendered %s hash mismatch; not uploading", artifact_type)
                        return None
                if expected.get("bytes") and path.stat().st_size != expected["bytes"]:
                    return None
                return str(path)
        candidate = self.artifact_files.get(artifact_type) or (
            settings.execution_resume_file if artifact_type == "RESUME" else settings.execution_cover_letter_file if artifact_type == "COVER_LETTER" else None
        )
        if not candidate:
            return None
        path = Path(candidate).expanduser()
        if not path.is_file() or path.stat().st_size == 0:
            return None
        return str(path)

    def _find_submit(self, page, strategy: Strategy, scan: PageScan):
        for selector in strategy.submit_locators:
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    return locator, selector
            except Exception:  # noqa: BLE001 - try the next candidate
                continue
        for button in scan.submit_buttons:
            if re.search(r"submit|apply|send", button.get("text", ""), re.IGNORECASE) or button.get("type") == "submit":
                try:
                    locator = page.locator(button["selector"]).first
                    if locator.count():
                        return locator, button["selector"]
                except Exception:  # noqa: BLE001
                    continue
        return None

    # --------------------------------------------------------------- handoffs

    def _wait_for_person(self, state: RunState, reason: HandoffReason) -> bool:
        """With a visible browser, give the person time to clear the challenge.

        No automation touches the challenge; we only re-scan until it is gone
        or the wait expires. Headless or wait 0: hand off immediately.
        """
        if self.session.headless or self.handoff_wait_seconds <= 0:
            return False
        if reason not in (HandoffReason.CAPTCHA_REQUIRED, HandoffReason.MFA_REQUIRED, HandoffReason.AUTH_REQUIRED):
            return False
        logger.info("Waiting up to %ss for the person to clear %s in the browser window", self.handoff_wait_seconds, reason.value)
        deadline = time.monotonic() + self.handoff_wait_seconds
        while time.monotonic() < deadline:
            try:
                state.page.wait_for_timeout(1000)
                target, rescan = self._settled_scan(state)
            except Exception:  # noqa: BLE001
                return False
            if self._handoff_condition(rescan, state.strategy) is None:
                state.target, state.scan = target, rescan
                state.handoff = None
                return True
        return False

    @staticmethod
    def _form_shape(fields: list[FormField]) -> list[tuple[str, str]]:
        """Which controls, in order: name (or label) and type — not values, not hint text."""
        return [((f.external_id or f.label or "").strip().lower(), f.field_type.value) for f in fields]

    def _require_mapped_form(self, form: FormSnapshot, state: RunState) -> None:
        """After a person cleared a wall, act only on the form whose fields were mapped."""
        current = state.scan.fields if state.scan is not None else []
        if self._form_shape(current) != self._form_shape(form.fields):
            raise ExecutorError(
                "the form on the page is not the one that was mapped (it appeared or changed while the person cleared the check); nothing more was filled and submit was not pressed: run the attempt again so the form is mapped first",
                before_submit=True,
                retryable=True,
                error_class=ErrorClass.FORM_CHANGED,
            )

    def _handoff_result(self, reason: HandoffReason, message: str, state: RunState, diagnostics: dict[str, Any], stopped_at: str, submit_attempted: bool = False, remaining: Optional[list[str]] = None) -> ExecutionResult:
        page_url = state.scan.url if state.scan else ""
        steps = remaining or {
            HandoffReason.CAPTCHA_REQUIRED: ["open the application page in your browser", "complete the human verification", "fill and submit the form from the prepared package", "confirm the outcome here"],
            HandoffReason.AUTH_REQUIRED: ["sign in to the employer site in your browser", "retry the attempt (a persistent local profile keeps you signed in)"],
            HandoffReason.MFA_REQUIRED: ["complete the two-factor step in your browser", "retry the attempt"],
            HandoffReason.UNSUPPORTED_FORM: ["apply through the page by hand using the prepared package", "confirm the outcome here"],
            HandoffReason.AMBIGUOUS_FORM: ["open the page and check the form", "apply by hand or retry"],
            HandoffReason.ARTIFACT_FILE_REQUIRED: ["provide the document file", "retry"],
        }.get(reason, ["continue by hand", "confirm the outcome"])
        self._debug_screenshot(state, reason)
        diagnostics = {**diagnostics, "handoff_page": page_url[:300], "steps": state.steps[-6:]}
        return ExecutionResult(outcome=ExecutionOutcome.HANDOFF, submit_attempted=submit_attempted, handoff_reason=reason, message=f"{message} (page: {page_url[:120]})", stopped_at=stopped_at, remaining_steps=steps, resumable=True, diagnostics=diagnostics)

    def _debug_screenshot(self, state: RunState, reason: HandoffReason) -> None:
        if not self.debug_artifacts_dir or state.page is None:
            return
        try:
            folder = Path(self.debug_artifacts_dir).expanduser()
            folder.mkdir(parents=True, exist_ok=True)
            name = f"handoff-{reason.value.lower()}-{utc_now().strftime('%Y%m%dT%H%M%S')}.png"
            state.page.screenshot(path=str(folder / name), full_page=False)
        except Exception:  # noqa: BLE001 - debugging aid only
            logger.debug("Screenshot failed", exc_info=True)

    # ---------------------------------------------------------- classification

    def _unknown(self, message: str, diagnostics: dict[str, Any], exc: Exception) -> ExecutionResult:
        error_class = ErrorClass.BROWSER_CRASH if _CRASH_RE.search(str(exc)) else ErrorClass.TIMEOUT if _TIMEOUT_RE.search(str(exc)) else ErrorClass.UNKNOWN
        if error_class is ErrorClass.BROWSER_CRASH:
            self._relaunch_after(exc)
        return ExecutionResult(outcome=ExecutionOutcome.UNKNOWN, submit_attempted=True, error_class=error_class, message=message, stopped_at="after_submit", diagnostics=diagnostics)

    def _classify_navigation(self, exc: Exception) -> ExecutorError:
        text = str(exc)
        if _CRASH_RE.search(text):
            self._relaunch_after(exc)
            return ExecutorError(f"browser crashed while navigating: {self._brief(exc)}", before_submit=True, retryable=True, error_class=ErrorClass.BROWSER_CRASH)
        if _TIMEOUT_RE.search(text):
            return ExecutorError(f"navigation timed out: {self._brief(exc)}", before_submit=True, retryable=True, error_class=ErrorClass.TIMEOUT)
        if _NET_RE.search(text):
            retryable = "ERR_NAME_NOT_RESOLVED" not in text and "ERR_FILE_NOT_FOUND" not in text
            return ExecutorError(f"navigation failed: {self._brief(exc)}", before_submit=True, retryable=retryable, error_class=ErrorClass.TRANSIENT_NETWORK if retryable else ErrorClass.TARGET_GONE)
        if "ERR_FILE_NOT_FOUND" in text or "404" in text:
            return ExecutorError(f"target gone: {self._brief(exc)}", before_submit=True, retryable=False, error_class=ErrorClass.TARGET_GONE)
        return ExecutorError(f"navigation error: {self._brief(exc)}", before_submit=True, retryable=True, error_class=ErrorClass.NAVIGATION)

    def _classify_fill(self, exc: Exception) -> ExecutorError:
        text = str(exc)
        if _CRASH_RE.search(text):
            self._relaunch_after(exc)
            return ExecutorError(f"browser crashed before submit: {self._brief(exc)}", before_submit=True, retryable=True, error_class=ErrorClass.BROWSER_CRASH)
        if _TIMEOUT_RE.search(text):
            return ExecutorError(f"a field could not be filled in time (form changed?): {self._brief(exc)}", before_submit=True, retryable=True, error_class=ErrorClass.FORM_CHANGED)
        return ExecutorError(f"filling failed: {self._brief(exc)}", before_submit=True, retryable=True, error_class=ErrorClass.EXECUTOR_CRASH)

    def _relaunch_after(self, exc: Exception) -> None:
        logger.warning("Browser-level failure (%s); relaunching", self._brief(exc))
        try:
            self.session.relaunch()
        except Exception:  # noqa: BLE001
            logger.exception("Browser relaunch failed")

    @staticmethod
    def _brief(exc: Exception) -> str:
        return str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__

    def _post_submit_evidence(self, package: ExecutionPackage):
        state = self._runs.get(package.application_id)
        if state is None or state.post_submit is None:
            return None
        return state.strategy, state.post_submit, state.post_submit_url

    def _close_state(self, application_id: str) -> None:
        state = self._runs.get(application_id)
        if state is None:
            return
        if state.page_cm is not None:
            try:
                state.page_cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.debug("Page close raised", exc_info=True)
        state.page = None
        state.page_cm = None
        state.target = None

    def close(self) -> None:
        for application_id in list(self._runs):
            self._close_state(application_id)
        self.session.close()
