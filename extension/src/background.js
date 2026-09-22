// Service worker: the extension's executor loop, one tab at a time.
//
//   tab URL -> /extension/match          (is this page a queued application?)
//   Fill    -> /extension/claim, /items/{id}/start, inject scripts, discover,
//              /items/{id}/form (server maps answers), fetch documents
//              (/documents/{id}/file?for_upload=true, SHA-256 re-checked), fill
//   submit  -> content script intercepts the click, /extension/gate, click
//   after   -> page evidence -> /items/{id}/result (server verifies)
//   pause   -> /items/{id}/handoff (CAPTCHA / login / MFA / unsupported)
//
// State lives in chrome.storage.session keyed by tab id. No candidate data is
// kept beyond the answers needed to fill the current page.
importScripts('api.js');

const {CareerOSApi, loadSettings, sha256Hex, toBase64} = CareerOS;
const CONTENT_FILES = ['src/discover.js', 'src/fill.js', 'src/content.js'];
const HEARTBEAT_ALARM = 'careeros-heartbeat';

// ------------------------------------------------------------------ state
async function getJob(tabId) {
  const key = 'job:' + tabId;
  const stored = await chrome.storage.session.get(key);
  return stored[key] || null;
}
async function setJob(tabId, job) {
  await chrome.storage.session.set({['job:' + tabId]: job});
  await updateBadge(tabId, job);
  return job;
}
async function patchJob(tabId, patch) {
  const job = (await getJob(tabId)) || {tabId: tabId};
  Object.assign(job, patch, {updatedAt: Date.now()});
  return setJob(tabId, job);
}
async function clearJob(tabId) {
  await chrome.storage.session.remove('job:' + tabId);
  await updateBadge(tabId, null);
}

const BADGES = {matched: ['1', '#1a73e8'], claimed: ['…', '#1a73e8'], filled: ['✓', '#188038'], awaiting_user: ['?', '#e37400'], submitted: ['…', '#188038'], settled: ['', '#188038'], handoff: ['!', '#d93025'], error: ['!', '#d93025']};
async function updateBadge(tabId, job) {
  const [text, color] = (job && BADGES[job.phase]) || ['', '#1a73e8'];
  try {
    await chrome.action.setBadgeText({tabId: tabId, text: text});
    await chrome.action.setBadgeBackgroundColor({tabId: tabId, color: color});
  } catch (error) { /* tab gone */ }
}

async function api() {
  const settings = await loadSettings();
  if (!settings.apiKey) throw new Error('No API key configured: open the extension options and paste the CareerOS API key');
  return {client: new CareerOSApi(settings), settings: settings};
}

// ------------------------------------------------------------- page bridge
async function ensureContent(tabId) {
  const probe = await sendToContent(tabId, {type: 'ping'});
  if (probe && probe.ok) return;
  await chrome.scripting.executeScript({target: {tabId: tabId}, files: CONTENT_FILES});
}
function sendToContent(tabId, message) {
  return new Promise((resolve) => {
    try {
      chrome.tabs.sendMessage(tabId, Object.assign({target: 'content'}, message), (reply) => {
        if (chrome.runtime.lastError) resolve({ok: false, error: chrome.runtime.lastError.message});
        else resolve(reply);
      });
    } catch (error) { resolve({ok: false, error: String(error)}); }
  });
}
async function content(tabId, message) {
  await ensureContent(tabId);
  const reply = await sendToContent(tabId, message);
  if (!reply || !reply.ok) throw new Error('page bridge: ' + ((reply && reply.error) || 'no reply'));
  return reply;
}

// ---------------------------------------------------------------- matching
async function matchTab(tabId, url) {
  if (!url || !/^https?:/.test(url)) return null;
  const existing = await getJob(tabId);
  if (existing && existing.phase && existing.phase !== 'matched' && existing.phase !== 'error') return existing;
  let match = null;
  try {
    const {client} = await api();
    match = await client.match(url);
  } catch (error) {
    return null;
  }
  if (!match) { if (existing) await clearJob(tabId); return null; }
  return setJob(tabId, {tabId: tabId, url: url, phase: 'matched', match: match, itemId: match.item.id, applicationId: match.application_id, title: match.title, company: match.company});
}

// ------------------------------------------------------------------ steps
function handoffFromScan(scan) {
  if (scan.captcha && scan.captcha_visible && !scan.captcha_solved) return ['CAPTCHA_REQUIRED', scan.captcha_inline ? 'the application form includes a human-verification widget' : 'a CAPTCHA / human-verification challenge is on the page', 'captcha', ['complete the human verification yourself (nothing is typed before that)', 'press Fill again']];
  if (scan.mfa) return ['MFA_REQUIRED', 'a one-time code prompt is on the page', 'mfa', ['enter the code yourself', 'press Fill again once the form is visible']];
  if (scan.login_wall) return ['AUTH_REQUIRED', 'the page asks for a login', 'login', ['sign in yourself', 'press Fill again once the form is visible']];
  if (!scan.fields.length) return ['UNSUPPORTED_FORM', 'no fillable form controls were found on this page', 'discover', ['open the application form', 'press Fill again']];
  return null;
}

function formFromScan(scan, settings) {
  return {
    source_url: scan.url,
    executor_kind: 'BROWSER_EXTENSION',
    executor_version: settings.executorVersion,
    fields: scan.fields.map(f => ({external_id: f.external_id, label: f.label || '', field_type: f.field_type, required: !!f.required, options: (f.options || []).map(o => ({label: o.label || '', value: o.value == null ? null : String(o.value)})), current_value: f.current_value == null ? null : String(f.current_value).slice(0, 1024), accept: f.accept || null, selector: f.selector, input_type: f.input_type})),
    metadata: {title: (scan.title || '').slice(0, 200), custom_widgets: scan.custom_widgets || 0, submit_buttons: (scan.submit_buttons || []).length, executor: 'browser_extension'},
  };
}

// The server answers positionally (fields are stored in the order sent).
function joinAnswers(scan, mapped) {
  const rows = mapped.snapshot.fields;
  return scan.fields.map((field, index) => {
    let row = rows[index] && rows[index].position === index ? rows[index] : rows.find(r => (r.external_id && r.external_id === field.external_id) || r.label === field.label);
    if (!row) return {label: field.label, selector: field.selector, field_type: field.field_type, required: !!field.required, status: 'SKIPPED', answer: null, selected_values: [], artifact_type: null};
    return {field_id: row.id, label: row.label, selector: field.selector, field_type: field.field_type, required: !!row.required, status: row.status, source: row.source, answer: row.answer, selected_values: row.selected_values || [], artifact_type: row.artifact_type, reason: row.reason, category: row.category};
  });
}

// Documents: fetched from the local API right before the upload and hashed
// again here, exactly like the Playwright executor re-hashes the file.
async function loadArtifacts(client, pkg, answers) {
  const wanted = new Set(answers.filter(a => a.status === 'ANSWERED' && a.field_type === 'file' && a.artifact_type).map(a => a.artifact_type));
  const declared = (pkg.execution_config && pkg.execution_config.artifacts) || {};
  const out = {};
  const problems = [];
  for (const type of wanted) {
    const meta = declared[type];
    if (!meta || !meta.id) { problems.push(type + ': no rendered document for this preparation'); continue; }
    try {
      const doc = await client.documentBytes(meta.id);
      const digest = await sha256Hex(doc.bytes);
      if (meta.sha256 && digest !== meta.sha256) { problems.push(type + ': document bytes do not match the recorded SHA-256; not uploaded'); continue; }
      if (doc.sha256 && digest !== doc.sha256) { problems.push(type + ': download hash mismatch; not uploaded'); continue; }
      if (!doc.bytes.length) { problems.push(type + ': empty document; not uploaded'); continue; }
      const name = (/filename="([^"]+)"/.exec(doc.disposition) || [null, type.toLowerCase() + '.' + (meta.format || 'pdf')])[1];
      out[type] = {base64: toBase64(doc.bytes), name: name, mime: doc.mime, sha256: digest, artifact_id: meta.id, version: meta.version};
    } catch (error) {
      problems.push(type + ': ' + error.message);
    }
  }
  return {artifacts: out, problems: problems};
}

async function runFill(tabId) {
  const {client, settings} = await api();
  const tab = await chrome.tabs.get(tabId);
  let job = await getJob(tabId);
  const url = tab.url;

  // 1. claim the item for this page (guarded on the server)
  if (!job || !job.itemId || job.phase === 'matched' || job.phase === 'error') {
    const match = await client.claim(url);
    if (!match) throw new Error('This page is not a queued application (or the queue item is not runnable yet)');
    job = await setJob(tabId, {tabId: tabId, url: url, phase: 'claimed', match: match, itemId: match.item.id, applicationId: match.application_id, title: match.title, company: match.company});
  }
  await chrome.alarms.create(HEARTBEAT_ALARM, {periodInMinutes: 1});

  // 2. start (server: idempotency, preconditions, documents, package, mutex)
  if (!job.runId) {
    const started = await client.start(job.itemId);
    if (started.outcome !== 'started') {
      await patchJob(tabId, {phase: 'settled', outcome: started.outcome, detail: started.detail, note: 'not started: ' + started.outcome + (started.detail && started.detail.codes ? ' (' + started.detail.codes.join(', ') + ')' : '')});
      return getJob(tabId);
    }
    job = await patchJob(tabId, {runId: started.run.id, package: {execution_config: started.package.execution_config, target: started.package.target, application_id: started.package.application_id}});
  }

  // 3. discover (structure only) and stop on anything that needs a person
  const discovered = await content(tabId, {type: 'discover'});
  const scan = discovered.scan;
  const handoff = handoffFromScan(scan);
  if (handoff) {
    const [reason, message, stoppedAt, remaining] = handoff;
    const outcome = await client.handoff(job.itemId, reason, message, stoppedAt, remaining);
    await content(tabId, {type: 'banner', text: message + ' — handed to you. Finish it yourself, then confirm in the popup.', kind: 'error'}).catch(() => null);
    return patchJob(tabId, {phase: 'handoff', handoffReason: reason, handoffMessage: message, outcome: outcome.outcome, remaining: remaining});
  }

  // 4. the server maps answers (one mapper for every executor)
  const mapped = await client.form(job.itemId, formFromScan(scan, settings));
  const answers = joinAnswers(scan, mapped);

  // 5. documents (only upload-eligible, hash-checked) and the fill itself
  const docs = await loadArtifacts(client, job.package, answers);
  const filled = await content(tabId, {type: 'fill', answers: answers, artifacts: docs.artifacts});
  const report = filled.report;
  const needsUser = answers.filter(a => a.status === 'NEEDS_USER_INPUT' || a.status === 'NEEDS_REVIEW');
  const missingFiles = report.file_missing.concat(docs.problems);
  const diagnostics = {fields_filled: report.filled.length, fields_skipped: report.skipped.length, missing_required: report.missing_required.slice(0, 10), uploads: Object.keys(docs.artifacts).map(k => ({type: k, artifact_id: docs.artifacts[k].artifact_id, version: docs.artifacts[k].version, sha256: docs.artifacts[k].sha256})), document_problems: docs.problems.slice(0, 5), custom_widgets: scan.custom_widgets || 0};

  if (missingFiles.length) {
    const outcome = await client.handoff(job.itemId, 'ARTIFACT_FILE_REQUIRED', 'a required upload has no usable rendered document: ' + missingFiles.slice(0, 2).join('; '), 'fill', ['attach the document yourself', 'submit and confirm in the popup']);
    return patchJob(tabId, {phase: 'handoff', handoffReason: 'ARTIFACT_FILE_REQUIRED', handoffMessage: missingFiles.join('; '), outcome: outcome.outcome, diagnostics: diagnostics});
  }

  const submitLocators = (scan.submit_buttons || []).map(b => b.selector);
  const mode = settings.submitMode || 'manual';
  if (mode === 'dry_run') {
    // Armed in dry_run mode: the page bridge blocks any submit with a banner.
    await content(tabId, {type: 'arm', mode: 'dry_run', locators: submitLocators}).catch(() => null);
    const outcome = await client.result(job.itemId, {outcome: 'DRY_RUN', submit_attempted: false, message: 'dry run: filled in the browser, submit deliberately not pressed', stopped_at: 'before_submit', diagnostics: diagnostics});
    await chrome.alarms.clear(HEARTBEAT_ALARM);
    return patchJob(tabId, {phase: 'settled', outcome: outcome.outcome, attemptStatus: outcome.attempt_status, diagnostics: diagnostics, note: 'dry run reported; nothing was submitted'});
  }

  await content(tabId, {type: 'arm', mode: mode, locators: submitLocators, wait_ms: settings.submitWaitMs});
  const pending = needsUser.map(a => ({field_id: a.field_id, label: a.label, reason: a.reason, status: a.status, required: a.required}));
  if (pending.length || report.missing_required.length) {
    return patchJob(tabId, {phase: 'awaiting_user', pending: pending, missing: report.missing_required, diagnostics: diagnostics, submitLocators: submitLocators, mode: mode, note: 'some fields need you; the submit stays gated'});
  }
  job = await patchJob(tabId, {phase: 'filled', diagnostics: diagnostics, submitLocators: submitLocators, mode: mode});
  if (mode === 'auto') return runSubmit(tabId);
  return job;
}

async function runSubmit(tabId) {
  const job = await getJob(tabId);
  if (!job || !job.runId) throw new Error('Fill the form first');
  if (job.phase === 'awaiting_user') throw new Error('Fields still need you: answer them in the popup or on the page');
  await content(tabId, {type: 'submit', locators: job.submitLocators || []});
  return getJob(tabId);
}

async function runAnswer(tabId, fieldId, answer, saveToBank) {
  const {client} = await api();
  await client.answerField(fieldId, answer, saveToBank);
  const job = await getJob(tabId);
  const pending = (job.pending || []).filter(p => p.field_id !== fieldId);
  // Re-map + re-fill so the new answer lands on the page through the same path.
  await patchJob(tabId, {pending: pending, phase: 'claimed'});
  return runFill(tabId);
}

async function onGate(tabId) {
  const job = await getJob(tabId);
  if (!job || !job.itemId) return {ok: false, failures: ['no active CareerOS application on this tab']};
  const {client, settings} = await api();
  let gate;
  try { gate = await client.gate(job.itemId); } catch (error) { return {ok: false, failures: [error.message]}; }
  if (!gate.ok) {
    const message = 'pre-submit gate failed: ' + gate.failures.slice(0, 3).join('; ');
    try {
      const outcome = await client.result(job.itemId, {outcome: 'NEEDS_REVIEW', submit_attempted: false, error_class: 'POLICY', message: message, stopped_at: 'before_submit', diagnostics: Object.assign({}, job.diagnostics || {}, {gate_failures: gate.failures.slice(0, 10)})});
      await patchJob(tabId, {phase: 'settled', outcome: outcome.outcome, attemptStatus: outcome.attempt_status, note: message});
    } catch (error) {
      await patchJob(tabId, {phase: 'error', note: message + ' / ' + error.message});
    }
    await chrome.alarms.clear(HEARTBEAT_ALARM);
    return {ok: false, failures: gate.failures};
  }
  await patchJob(tabId, {phase: 'submitted', submittedAt: Date.now(), urlBefore: job.url, waitMs: settings.submitWaitMs});
  await chrome.alarms.create('careeros-settle:' + tabId, {delayInMinutes: 1});
  return {ok: true};
}

async function onReport(tabId, result) {
  const job = await getJob(tabId);
  if (!job || !job.itemId || job.phase === 'settled') return {ok: false, error: 'nothing to report'};
  const {client} = await api();
  const merged = Object.assign({}, result, {diagnostics: Object.assign({}, job.diagnostics || {}, result.diagnostics || {})});
  let outcome;
  try { outcome = await client.result(job.itemId, merged); }
  catch (error) { await patchJob(tabId, {phase: 'error', note: 'result not accepted: ' + error.message}); return {ok: false, error: error.message}; }
  await chrome.alarms.clear(HEARTBEAT_ALARM);
  await chrome.alarms.clear('careeros-settle:' + tabId);
  await patchJob(tabId, {phase: result.outcome === 'HANDOFF' ? 'handoff' : 'settled', outcome: outcome.outcome, attemptStatus: outcome.attempt_status, queueState: outcome.queue_state, handoffReason: result.handoff_reason || null, handoffMessage: result.message || null, note: result.message});
  return {ok: true, outcome: outcome};
}

async function runConfirm(tabId, submitted, reference, note) {
  const job = await getJob(tabId);
  if (!job || !job.runId) throw new Error('No execution run to confirm');
  const {client} = await api();
  const run = await client.confirm(job.runId, submitted, reference, note);
  await chrome.alarms.clear(HEARTBEAT_ALARM);
  await patchJob(tabId, {phase: 'settled', outcome: run.status, note: 'confirmed by you: ' + (submitted ? 'submitted' : 'not submitted')});
  return run;
}

async function runPause(tabId, reason, message) {
  const job = await getJob(tabId);
  if (!job || !job.itemId || !job.runId) throw new Error('Nothing to pause');
  const {client} = await api();
  const outcome = await client.handoff(job.itemId, reason || 'USER_CONFIRMATION_REQUIRED', message || 'paused by the candidate from the extension', 'popup', ['finish in the browser', 'confirm in the popup']);
  await content(tabId, {type: 'disarm'}).catch(() => null);
  return patchJob(tabId, {phase: 'handoff', handoffReason: reason || 'USER_CONFIRMATION_REQUIRED', handoffMessage: message, outcome: outcome.outcome});
}

// -------------------------------------------------------------- listeners
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  // Messages come only from this extension (its content script, popup, options);
  // no externally_connectable is declared, and this guard makes it explicit.
  if (!sender || sender.id !== chrome.runtime.id) return false;
  const tabId = (sender.tab && sender.tab.id) || message.tabId;
  const respond = (promise) => promise.then(sendResponse, (error) => sendResponse({ok: false, error: error.message || String(error)}));
  switch (message && message.type) {
    case 'gate': respond(onGate(tabId)); return true;
    case 'report': respond(onReport(tabId, message.result)); return true;
    case 'popup:state': respond((async () => { const tab = await chrome.tabs.get(tabId); const job = await matchTab(tabId, tab.url); return {ok: true, job: job, url: tab.url}; })()); return true;
    case 'popup:fill': respond(runFill(tabId).then(job => ({ok: true, job: job}))); return true;
    case 'popup:submit': respond(runSubmit(tabId).then(job => ({ok: true, job: job}))); return true;
    case 'popup:answer': respond(runAnswer(tabId, message.field_id, message.answer, message.save_to_bank).then(job => ({ok: true, job: job}))); return true;
    case 'popup:confirm': respond(runConfirm(tabId, message.submitted, message.reference, message.note).then(run => ({ok: true, run: run}))); return true;
    case 'popup:pause': respond(runPause(tabId, message.reason, message.message).then(job => ({ok: true, job: job}))); return true;
    case 'popup:reset': respond(clearJob(tabId).then(() => ({ok: true}))); return true;
    case 'popup:status': respond((async () => { const {client, settings} = await api(); const status = await client.status(); return {ok: true, status: status, settings: {baseUrl: settings.baseUrl, submitMode: settings.submitMode, workerId: settings.workerId}}; })()); return true;
    default: return false;
  }
});

chrome.tabs.onUpdated.addListener(async (tabId, info, tab) => {
  if (info.status !== 'complete' || !tab.url) return;
  const job = await getJob(tabId);
  if (job && job.phase === 'submitted') {
    // The submit navigated: the content script died with the old page. Look at
    // the new one for evidence (needs activeTab to still cover this origin).
    try {
      await chrome.scripting.executeScript({target: {tabId: tabId}, files: CONTENT_FILES});
      const reply = await sendToContent(tabId, {target: 'content', type: 'classify', url_before: job.urlBefore});
      if (reply && reply.ok && reply.result) { await onReport(tabId, reply.result); return; }
      await sendToContent(tabId, {target: 'content', type: 'observe', url_before: job.urlBefore, wait_ms: Math.max(1000, (job.waitMs || 15000) - (Date.now() - job.submittedAt))});
    } catch (error) {
      // Cannot see the new page (different origin, no permission): unknown, never resubmitted.
      await onReport(tabId, {outcome: 'UNKNOWN', submit_attempted: true, error_class: 'TIMEOUT', message: 'page changed after submit and could not be inspected: ' + error.message, stopped_at: 'after_submit', application_url: tab.url});
    }
    return;
  }
  if (!job || job.phase === 'matched' || job.phase === 'settled' || job.phase === 'error') await matchTab(tabId, tab.url);
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const job = await getJob(tabId);
  if (job && job.phase === 'submitted') {
    await onReport(tabId, {outcome: 'UNKNOWN', submit_attempted: true, error_class: 'TIMEOUT', message: 'the tab was closed right after submit; outcome unknown', stopped_at: 'after_submit'});
  }
  await chrome.storage.session.remove('job:' + tabId);
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === HEARTBEAT_ALARM) {
    const all = await chrome.storage.session.get(null);
    let active = 0;
    for (const [key, job] of Object.entries(all)) {
      if (!key.startsWith('job:') || !job.itemId || ['settled', 'error', 'matched', 'handoff'].includes(job.phase)) continue;
      active += 1;
      try { const {client} = await api(); await client.heartbeat(job.itemId); } catch (error) { /* lease may have lapsed; the server refuses late results */ }
    }
    if (!active) await chrome.alarms.clear(HEARTBEAT_ALARM);
    return;
  }
  if (alarm.name.startsWith('careeros-settle:')) {
    const tabId = Number(alarm.name.split(':')[1]);
    const job = await getJob(tabId);
    if (job && job.phase === 'submitted') {
      await onReport(tabId, {outcome: 'UNKNOWN', submit_attempted: true, error_class: 'TIMEOUT', message: 'no result observed within a minute of submit; outcome unknown', stopped_at: 'after_submit'});
    }
  }
});
