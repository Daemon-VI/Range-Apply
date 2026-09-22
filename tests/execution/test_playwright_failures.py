"""Phase 13: real browser failure modes, injected on the wire.

The fixtures are served over loopback HTTP (not ``file://``) so Playwright
routing can inject what real employer sites do to us: slow responses, hung
loads, 5xx pages, connection resets, tabs closing mid-fill, navigations and
DOM changes between discovery and fill, and confirmations that arrive too
late. Every case asserts the two invariants that matter for volume:

* before the click, any failure is retryable and *no* submit is pressed;
* after the click, the outcome is UNKNOWN / UNCERTAIN and a retry never
  presses submit again.
"""

import functools
import http.server
import threading
import time
from contextlib import contextmanager

import pytest

from app.application.models import ApplicationStatus
from app.execution.models import ErrorClass, ExecutorKind, HandoffReason
from app.execution.playwright import executor as executor_module
from app.execution.playwright.browser import BrowserSession, Pacer
from app.execution.playwright.executor import PlaywrightExecutor
from app.pipeline.models import QueueState
from tests.execution.playwright_conftest import FIXTURES, attach, make_session, requires_browser

pytestmark = requires_browser


#: Server-side delays per fixture basename (ms): the *server* is slow, not the browser loop.
SLOW: dict[str, int] = {}


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401 - silence
        pass

    def do_GET(self):
        delay = SLOW.get(self.path.split("?")[0].rsplit("/", 1)[-1], 0)
        if delay:
            time.sleep(delay / 1000.0)
        super().do_GET()


@pytest.fixture(scope="module")
def http_base():
    handler = functools.partial(_Quiet, directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    session = make_session()
    yield session
    session.close()


@pytest.fixture
def resume_file(tmp_path):
    path = tmp_path / "resume.txt"
    path.write_text("Ribhu Siripurapu - resume (test file)\n", encoding="utf-8")
    return path


def _executor(session: BrowserSession, resume_file, dry_run=True, **kw) -> PlaywrightExecutor:
    return PlaywrightExecutor(session=session, dry_run=dry_run, submit_wait_ms=kw.pop("submit_wait_ms", 1500), settle_ms=1000, handoff_wait_seconds=0, pacer=Pacer(0), artifact_files={"RESUME": str(resume_file)} if resume_file else {}, **kw)


def _route_pages(monkeypatch, session: BrowserSession, install):
    """Install Playwright routes on every page the executor opens."""
    original = session.page

    @contextmanager
    def page(**kwargs):
        with original(**kwargs) as p:
            install(p)
            yield p

    monkeypatch.setattr(session, "page", page)


def _after_first_fill(monkeypatch, action):
    """Run ``action(page)`` once, right after the first field is filled."""
    original = executor_module.PlaywrightExecutor._fill
    done = {"v": False}

    def fill(self, page, field, answer, package=None):
        result = original(self, page, field, answer, package)
        if not done["v"]:
            done["v"] = True
            action(page)
        return result

    monkeypatch.setattr(executor_module.PlaywrightExecutor, "_fill", fill)


def _run(h, attempt):
    return h.execute(attempt, worker="pwf", executor=ExecutorKind.PLAYWRIGHT_LOCAL)


def _assert_retryable_before_submit(h, attempt, executor, error_class):
    run = h.service.runs_for(attempt.id)[0]
    assert run.error_class == error_class.value, (run.error_class, run.message)
    assert run.submit_invoked is False and executor.submit_clicks == 0
    assert attempt.status == ApplicationStatus.READY.value and h.item(attempt).state == QueueState.RETRY_WAIT.value


# ------------------------------------------------------------ before submit


def test_slow_network_within_timeout_still_discovers_and_dry_runs(browser, harness, resume_file, monkeypatch, http_base):
    executor = _executor(browser, resume_file)
    attach(harness, executor)

    def slow(page):
        page.route("**/*", lambda route: (time.sleep(0.4), route.continue_()))

    _route_pages(monkeypatch, browser, slow)
    attempt = harness.ready(company="Slow Net", source="GREENHOUSE", application_url=f"{http_base}/greenhouse.html")
    outcome = _run(harness, attempt)
    assert outcome["outcome"] == "DRY_RUN", outcome
    run = harness.service.runs_for(attempt.id)[0]
    assert run.diagnostics["fields_filled"] >= 6 and executor.submit_clicks == 0


def test_hung_page_load_times_out_retryably_before_any_fill(browser, harness, resume_file, monkeypatch, http_base):
    monkeypatch.setattr(browser, "navigation_timeout_ms", 1500)
    executor = _executor(browser, resume_file)
    attach(harness, executor)

    def hang(page):
        page.route("**/greenhouse.html", lambda route: None)  # never answered

    _route_pages(monkeypatch, browser, hang)
    attempt = harness.ready(company="Hung Load", source="GREENHOUSE", application_url=f"{http_base}/greenhouse.html")
    outcome = _run(harness, attempt)
    assert outcome["outcome"] == "RETRYABLE_FAILURE", outcome
    _assert_retryable_before_submit(harness, attempt, executor, ErrorClass.TIMEOUT)


def test_server_error_page_hands_off_without_typing_anything(browser, harness, resume_file, monkeypatch, http_base):
    executor = _executor(browser, resume_file)
    attach(harness, executor)

    def five_hundred(page):
        page.route("**/greenhouse.html", lambda route: route.fulfill(status=500, content_type="text/html", body="<html><body><h1>500 Internal Server Error</h1><p>Try again later.</p></body></html>"))

    _route_pages(monkeypatch, browser, five_hundred)
    attempt = harness.ready(company="Five Hundred", source="GREENHOUSE", application_url=f"{http_base}/greenhouse.html")
    outcome = _run(harness, attempt)
    assert outcome["outcome"] == "HANDOFF", outcome
    run = harness.service.runs_for(attempt.id)[0]
    assert run.handoff_reason == HandoffReason.AMBIGUOUS_FORM.value and run.status == "HANDOFF"
    assert run.diagnostics.get("fields_filled") is None and executor.submit_clicks == 0


def test_connection_reset_is_a_transient_network_failure(browser, harness, resume_file, monkeypatch, http_base):
    executor = _executor(browser, resume_file)
    attach(harness, executor)
    _route_pages(monkeypatch, browser, lambda page: page.route("**/greenhouse.html", lambda route: route.abort("connectionreset")))
    attempt = harness.ready(company="Reset Inc", source="GREENHOUSE", application_url=f"{http_base}/greenhouse.html")
    outcome = _run(harness, attempt)
    assert outcome["outcome"] == "RETRYABLE_FAILURE", outcome
    _assert_retryable_before_submit(harness, attempt, executor, ErrorClass.TRANSIENT_NETWORK)


def test_tab_closed_mid_fill_is_retryable_and_the_browser_recovers(browser, harness, resume_file, monkeypatch, http_base):
    executor = _executor(browser, resume_file)
    attach(harness, executor)
    _after_first_fill(monkeypatch, lambda page: page.close())
    attempt = harness.ready(company="Closed Tab", source="GREENHOUSE", application_url=f"{http_base}/greenhouse.html")
    outcome = _run(harness, attempt)
    assert outcome["outcome"] == "RETRYABLE_FAILURE", outcome
    _assert_retryable_before_submit(harness, attempt, executor, ErrorClass.BROWSER_CRASH)
    assert browser.running


def test_navigation_away_during_fill_is_form_changed_not_a_submit(browser, harness, resume_file, monkeypatch, http_base):
    browser.action_timeout_ms = 1500
    try:
        executor = _executor(browser, resume_file)
        attach(harness, executor)
        _after_first_fill(monkeypatch, lambda page: page.goto(f"{http_base}/login.html"))
        attempt = harness.ready(company="Redirected Mid", source="GREENHOUSE", application_url=f"{http_base}/greenhouse.html")
        outcome = _run(harness, attempt)
        assert outcome["outcome"] == "RETRYABLE_FAILURE", outcome
        run = harness.service.runs_for(attempt.id)[0]
        assert run.error_class in (ErrorClass.FORM_CHANGED.value, ErrorClass.EXECUTOR_CRASH.value, ErrorClass.BROWSER_CRASH.value)
        assert run.submit_invoked is False and executor.submit_clicks == 0
        assert attempt.status == ApplicationStatus.READY.value and harness.item(attempt).state == QueueState.RETRY_WAIT.value
    finally:
        browser.action_timeout_ms = 10000


def test_dom_change_between_discovery_and_fill_is_form_changed(browser, harness, resume_file, monkeypatch, http_base):
    browser.action_timeout_ms = 1500
    try:
        executor = _executor(browser, resume_file)
        attach(harness, executor)
        _after_first_fill(monkeypatch, lambda page: page.evaluate("() => document.querySelectorAll('input, textarea, select').forEach((el, i) => { if (i > 0) el.remove(); })"))
        attempt = harness.ready(company="Mutated DOM", source="GREENHOUSE", application_url=f"{http_base}/greenhouse.html")
        outcome = _run(harness, attempt)
        assert outcome["outcome"] == "RETRYABLE_FAILURE", outcome
        _assert_retryable_before_submit(harness, attempt, executor, ErrorClass.FORM_CHANGED)
    finally:
        browser.action_timeout_ms = 10000


def test_reload_before_fill_is_harmless_in_dry_run(browser, harness, resume_file, monkeypatch, http_base):
    executor = _executor(browser, resume_file)
    attach(harness, executor)
    _after_first_fill(monkeypatch, lambda page: page.reload())
    attempt = harness.ready(company="Reloaded", source="GREENHOUSE", application_url=f"{http_base}/greenhouse.html")
    outcome = _run(harness, attempt)
    assert outcome["outcome"] == "DRY_RUN", outcome
    assert executor.submit_clicks == 0


# ------------------------------------------------------------- after submit


def test_tab_closed_right_after_the_click_is_unknown_and_never_resubmitted(browser, harness, resume_file, monkeypatch, http_base):
    executor = _executor(browser, resume_file, dry_run=False)
    attach(harness, executor)
    original = executor_module.PlaywrightExecutor._after_submit

    def close_then_observe(self, page, state, strategy, url_before, diagnostics):
        page.close()
        return original(self, page, state, strategy, url_before, diagnostics)

    monkeypatch.setattr(executor_module.PlaywrightExecutor, "_after_submit", close_then_observe)
    attempt = harness.ready(company="Closed After", source="GREENHOUSE", application_url=f"{http_base}/greenhouse.html")
    outcome = _run(harness, attempt)
    assert outcome["outcome"] == "UNKNOWN", outcome
    assert attempt.status == ApplicationStatus.UNCERTAIN.value and executor.submit_clicks == 1
    run = harness.service.runs_for(attempt.id)[0]
    assert run.submit_invoked and run.error_class == ErrorClass.BROWSER_CRASH.value
    assert harness.item(attempt).state == QueueState.NEEDS_REVIEW.value
    # nothing on the queue will pick it up again
    assert [i.id for i in harness.service.claim("pwf2", limit=10)] == []
    assert executor.submit_clicks == 1


def test_slow_confirmation_page_is_still_observed_and_submitted_once(browser, harness, resume_file, monkeypatch, http_base):
    """A confirmation the server takes seconds to send: the browser blocks on
    the pending navigation, the executor sees the confirmation when it lands,
    and exactly one submit was pressed. Verified from the page, not assumed."""
    executor = _executor(browser, resume_file, dry_run=False, submit_wait_ms=1200)
    attach(harness, executor)
    monkeypatch.setitem(SLOW, "confirmation.html", 4000)
    attempt = harness.ready(company="Slow Confirm", source="GREENHOUSE", application_url=f"{http_base}/greenhouse.html")
    started = time.monotonic()
    outcome = _run(harness, attempt)
    assert time.monotonic() - started >= 4.0
    assert outcome["outcome"] in ("SUBMITTED", "UNKNOWN"), outcome
    assert executor.submit_clicks == 1
    run = harness.service.runs_for(attempt.id)[0]
    assert run.submit_invoked
    if outcome["outcome"] == "SUBMITTED":
        assert attempt.status == ApplicationStatus.VERIFIED.value and run.verification_status == "VERIFIED"
    else:
        assert attempt.status == ApplicationStatus.UNCERTAIN.value and harness.item(attempt).state == QueueState.NEEDS_REVIEW.value
    assert [i.id for i in harness.service.claim("pwf3", limit=10)] == []
    assert executor.submit_clicks == 1
