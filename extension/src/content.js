// In-page bridge for the CareerOS extension. Injected on demand (activeTab)
// together with discover.js and fill.js. It never decides anything about the
// application on its own:
//   * discovery reports structure to the server, which maps the answers;
//   * filling applies exactly those answers;
//   * every submit (the person's click or the extension's) is intercepted and
//     only proceeds after the server-side gate says so;
//   * afterwards it observes the page and reports only what it saw.
//
// `bridge` abstracts messaging so the same code runs under Playwright in the
// Python tests with a stub bridge (globalThis.careerosBridge).
(function () {
  if (globalThis.__careerosContent) return;

  const SUCCESS = /thank you for (applying|your application|submitting)|application (has been |was )?(submitted|received|sent)|we('ve| have) received your application|successfully (submitted|applied)|your application is (in|complete)|applied successfully/i;
  const REFERENCE = /(?:application|reference|confirmation|tracking)\s*(?:id|number|no\.?|#|code)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-]{3,})/i;
  const VALIDATION = /(is required|required field|please (fill|complete|enter|select|provide)|invalid|must be|can't be blank|cannot be blank|field is missing)/i;

  const state = {armed: false, mode: 'manual', gatePassed: false, urlBefore: location.href, submitLocators: [], observer: null, log: []};

  const bridge = globalThis.careerosBridge || {
    send: (message) => new Promise((resolve) => {
      try { chrome.runtime.sendMessage(message, (reply) => resolve(chrome.runtime.lastError ? {ok: false, error: chrome.runtime.lastError.message} : reply)); }
      catch (error) { resolve({ok: false, error: String(error)}); }
    }),
  };

  function evidence(scan) {
    const body = scan.body_excerpt || '';
    const success = SUCCESS.exec(body);
    const reference = REFERENCE.exec(body);
    const errors = (scan.errors || []).filter(e => VALIDATION.test(e));
    return {url: scan.url, success_marker: success ? success[0] : null, reference: reference ? reference[1] : null, validation_errors: errors.length ? errors : (scan.errors || []), captcha: !!scan.captcha_visible, captcha_invisible: !!scan.captcha && !scan.captcha_visible, mfa: !!scan.mfa, login_wall: !!scan.login_wall};
  }

  function banner(text, kind) {
    let box = document.getElementById('careeros-banner');
    if (!box) {
      box = document.createElement('div');
      box.id = 'careeros-banner';
      box.setAttribute('role', 'status');
      box.style.cssText = 'position:fixed;top:12px;right:12px;z-index:2147483647;max-width:360px;padding:10px 14px;border-radius:8px;font:13px/1.4 system-ui,sans-serif;box-shadow:0 4px 16px rgba(0,0,0,.2);color:#111;background:#e8f1ff;border:1px solid #9cc0ff';
      document.documentElement.appendChild(box);
    }
    box.style.background = kind === 'error' ? '#ffe8e8' : kind === 'ok' ? '#e6f7e6' : '#e8f1ff';
    box.style.borderColor = kind === 'error' ? '#ff9c9c' : kind === 'ok' ? '#8fd18f' : '#9cc0ff';
    box.textContent = 'CareerOS: ' + text;
  }

  function isSubmitControl(el) {
    if (!el) return false;
    const tag = el.tagName;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'INPUT') return type === 'submit';
    if (tag !== 'BUTTON') return false;
    if (type === 'button' || type === 'reset') return false;
    return true;
  }

  // ---- submit interception: nothing leaves the page before the gate --------
  function arm(mode, locators) {
    state.mode = mode || 'manual';
    state.submitLocators = locators || [];
    state.gatePassed = false;
    state.urlBefore = location.href;
    if (state.armed) return;
    state.armed = true;
    document.addEventListener('submit', onSubmit, true);
    document.addEventListener('click', onClick, true);
  }

  function disarm() {
    if (!state.armed) return;
    state.armed = false;
    document.removeEventListener('submit', onSubmit, true);
    document.removeEventListener('click', onClick, true);
  }

  function onSubmit(event) {
    if (!state.armed || state.gatePassed) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const form = event.target;
    const submitter = event.submitter || null;
    gateThen(() => { if (form.requestSubmit) form.requestSubmit(submitter || undefined); else form.submit(); });
  }

  function onClick(event) {
    if (!state.armed || state.gatePassed) return;
    const control = event.target && event.target.closest ? event.target.closest('button, input[type="submit"]') : null;
    if (!isSubmitControl(control)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    gateThen(() => control.click());
  }

  let gating = false;
  async function gateThen(proceed) {
    if (gating) return;
    if (state.mode === 'dry_run') {
      // The person chose dry run: nothing leaves this page through us.
      banner('dry-run mode: submit blocked. Change the submit mode in the extension options to apply.', 'error');
      state.log.push({gate: 'dry_run_blocked'});
      return;
    }
    gating = true;
    banner('checking with the local server before submitting…');
    let reply;
    try { reply = await bridge.send({type: 'gate'}); } finally { gating = false; }
    if (!reply || !reply.ok) {
      const failures = (reply && reply.failures) || [(reply && reply.error) || 'gate unavailable'];
      banner('not submitted: ' + failures.join('; '), 'error');
      state.log.push({gate: 'refused', failures: failures});
      return;
    }
    state.gatePassed = true;
    state.log.push({gate: 'passed'});
    banner('submitting…', 'ok');
    startObserving();
    try { proceed(); } catch (error) { bridge.send({type: 'report', result: unknownResult('submit action failed in page: ' + error.message)}); }
  }

  // ---- after the click: observe, never assume ----------------------------
  function unknownResult(message) {
    return {outcome: 'UNKNOWN', submit_attempted: true, error_class: 'TIMEOUT', message: message, stopped_at: 'after_submit', application_url: location.href, diagnostics: {result_url: location.href.slice(0, 300)}};
  }

  function classify(urlBefore) {
    const ev = evidence(careerosDiscover());
    if (ev.success_marker || ev.reference) {
      return {outcome: 'SUBMITTED', submit_attempted: true, application_url: ev.url, external_application_id: ev.reference, confirmation_reference: ev.reference, message: 'confirmation text observed after submit', diagnostics: {success_marker: ev.success_marker, result_url: ev.url.slice(0, 300)}};
    }
    if (ev.captcha) {
      return {outcome: 'HANDOFF', submit_attempted: true, handoff_reason: 'CAPTCHA_REQUIRED', message: 'a CAPTCHA appeared after pressing submit', stopped_at: 'after_submit', remaining_steps: ['complete the challenge', 'confirm whether the application went through'], application_url: ev.url};
    }
    if (ev.validation_errors.length && ev.url === urlBefore) {
      return {outcome: 'NEEDS_REVIEW', submit_attempted: true, error_class: 'VALIDATION', message: 'the form rejected the submission: ' + ev.validation_errors.slice(0, 3).join('; '), stopped_at: 'after_submit', application_url: ev.url, diagnostics: {validation_errors: ev.validation_errors.slice(0, 5)}};
    }
    return null;
  }

  function startObserving(urlBefore, waitMs) {
    const before = urlBefore || state.urlBefore;
    const deadline = Date.now() + (waitMs || state.waitMs || 15000);
    if (state.observer) clearInterval(state.observer);
    state.observer = setInterval(() => {
      let result = null;
      try { result = classify(before); } catch (error) { result = null; }
      if (result) {
        clearInterval(state.observer); state.observer = null;
        bridge.send({type: 'report', result: result});
        banner(result.outcome === 'SUBMITTED' ? 'submission confirmed on the page' : result.message, result.outcome === 'SUBMITTED' ? 'ok' : 'error');
      } else if (Date.now() > deadline) {
        clearInterval(state.observer); state.observer = null;
        bridge.send({type: 'report', result: unknownResult('no confirmation, error or redirect observed after submit; outcome unknown')});
        banner('could not tell whether the application went through; verify it in CareerOS', 'error');
      }
    }, 250);
  }

  // ---- commands from the service worker ----------------------------------
  const handlers = {
    ping: () => ({ok: true, url: location.href}),
    discover: () => {
      const scan = careerosDiscover();
      return {ok: true, scan: scan, evidence: evidence(scan)};
    },
    fill: (message) => {
      const report = careerosFill(message.answers || [], message.artifacts || {});
      banner('filled ' + report.filled.length + ' field(s)' + (report.missing_required.length ? '; ' + report.missing_required.length + ' required field(s) need you' : ''), report.missing_required.length ? 'info' : 'ok');
      return {ok: true, report: report};
    },
    arm: (message) => { state.waitMs = message.wait_ms || state.waitMs; arm(message.mode, message.locators); return {ok: true}; },
    disarm: () => { disarm(); return {ok: true}; },
    submit: (message) => {
      const button = careerosFindSubmit(message.locators || state.submitLocators);
      if (!button) return {ok: false, error: 'no submit button found'};
      arm(state.mode, state.submitLocators);
      gateThen(() => button.click());
      return {ok: true};
    },
    observe: (message) => { state.waitMs = message.wait_ms || state.waitMs; startObserving(message.url_before, message.wait_ms); return {ok: true}; },
    classify: (message) => ({ok: true, result: classify(message.url_before || state.urlBefore)}),
    banner: (message) => { banner(message.text, message.kind); return {ok: true}; },
    state: () => ({ok: true, armed: state.armed, gatePassed: state.gatePassed, mode: state.mode, log: state.log}),
  };

  function handle(message) {
    const handler = handlers[message && message.type];
    if (!handler) return {ok: false, error: 'unknown command ' + (message && message.type)};
    try { return handler(message); } catch (error) { return {ok: false, error: String(error && error.message || error)}; }
  }

  if (!globalThis.careerosBridge && typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.onMessage) {
    chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
      // Only this extension's own service worker may drive the page bridge.
      if (!sender || sender.id !== chrome.runtime.id) return false;
      if (!message || message.target !== 'content') return false;
      sendResponse(handle(message));
      return false;
    });
  }

  globalThis.__careerosContent = {handle, state, evidence, classify};
})();
