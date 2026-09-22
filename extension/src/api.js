// Thin client for the local CareerOS API. The only thing the extension ever
// talks to. The API key is entered by the person in the options page and
// kept in chrome.storage.local (never in this source, never in a page).
//
// Loaded by the service worker (importScripts) and by the popup / options
// pages (<script>), so it is a plain global, not a module.
(function (root) {
  const DEFAULTS = {
    baseUrl: 'http://127.0.0.1:8000',
    apiKey: '',
    workerId: '',
    submitMode: 'manual',      // manual | auto | dry_run
    submitWaitMs: 15000,
    executorVersion: 'extension-0.1.0',
  };

  class ApiError extends Error {
    constructor(status, detail, payload) {
      super(detail || ('HTTP ' + status));
      this.status = status;
      this.payload = payload || null;
    }
  }

  async function loadSettings() {
    const stored = await chrome.storage.local.get(Object.keys(DEFAULTS));
    const settings = Object.assign({}, DEFAULTS, stored);
    if (!settings.workerId) {
      settings.workerId = 'ext-' + Math.random().toString(36).slice(2, 10);
      await chrome.storage.local.set({workerId: settings.workerId});
    }
    settings.baseUrl = String(settings.baseUrl || DEFAULTS.baseUrl).replace(/\/+$/, '');
    return settings;
  }

  async function saveSettings(patch) {
    const clean = {};
    for (const key of Object.keys(DEFAULTS)) if (key in patch) clean[key] = patch[key];
    if (clean.baseUrl) clean.baseUrl = String(clean.baseUrl).replace(/\/+$/, '');
    await chrome.storage.local.set(clean);
    return loadSettings();
  }

  function isLocalUrl(baseUrl) {
    try {
      const u = new URL(baseUrl);
      return u.protocol === 'http:' && (u.hostname === '127.0.0.1' || u.hostname === 'localhost' || u.hostname === '[::1]');
    } catch (error) { return false; }
  }

  class CareerOSApi {
    constructor(settings) {
      this.settings = settings;
      if (!isLocalUrl(settings.baseUrl)) throw new ApiError(0, 'CareerOS must be reached on localhost (http://127.0.0.1:PORT); refusing ' + settings.baseUrl);
    }

    async request(method, path, body, options) {
      const headers = {'X-API-Key': this.settings.apiKey || ''};
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      let response;
      try {
        response = await fetch(this.settings.baseUrl + path, {method: method, headers: headers, body: body === undefined ? undefined : JSON.stringify(body)});
      } catch (error) {
        throw new ApiError(0, 'CareerOS is not reachable at ' + this.settings.baseUrl + ' (' + error.message + ')');
      }
      if (options && options.raw) return response;
      const text = await response.text();
      let payload = null;
      try { payload = text ? JSON.parse(text) : null; } catch (error) { payload = null; }
      if (!response.ok) throw new ApiError(response.status, (payload && payload.detail) || ('HTTP ' + response.status), payload);
      return payload;
    }

    get(path) { return this.request('GET', path); }
    post(path, body) { return this.request('POST', path, body === undefined ? {} : body); }

    // ---- execution API (Phase 6/7/9) ------------------------------------
    status() { return this.get('/api/v1/execution/extension/status?worker_id=' + encodeURIComponent(this.settings.workerId)); }
    match(url) { return this.get('/api/v1/execution/extension/match?url=' + encodeURIComponent(url) + '&worker_id=' + encodeURIComponent(this.settings.workerId)); }
    claim(url) { return this.post('/api/v1/execution/extension/claim', {url: url, worker_id: this.settings.workerId}); }
    heartbeat(itemId) { return this.post('/api/v1/execution/extension/heartbeat', {item_id: itemId, worker_id: this.settings.workerId}); }
    gate(itemId) { return this.post('/api/v1/execution/extension/gate', {item_id: itemId, worker_id: this.settings.workerId}); }
    start(itemId) { return this.post('/api/v1/execution/items/' + itemId + '/start', {worker_id: this.settings.workerId, executor: 'BROWSER_EXTENSION'}); }
    form(itemId, form) { return this.post('/api/v1/execution/items/' + itemId + '/form', {worker_id: this.settings.workerId, form: form}); }
    result(itemId, result) { return this.post('/api/v1/execution/items/' + itemId + '/result', {worker_id: this.settings.workerId, result: result}); }
    handoff(itemId, reason, message, stoppedAt, remaining) { return this.post('/api/v1/execution/items/' + itemId + '/handoff', {worker_id: this.settings.workerId, reason: reason, message: message, stopped_at: stoppedAt || null, remaining_steps: remaining || []}); }
    confirm(runId, submitted, reference, note) { return this.post('/api/v1/execution/runs/' + runId + '/confirm', {submitted: submitted, reference: reference || null, note: note || null}); }
    answerField(fieldId, answer, saveToBank) { return this.post('/api/v1/execution/fields/' + fieldId + '/answer', {answer: answer, save_to_bank: !!saveToBank}); }
    run(runId) { return this.get('/api/v1/execution/runs/' + runId); }

    // ---- documents (Phase 8): bytes of an upload-eligible rendered artifact
    async documentBytes(artifactId) {
      const response = await this.request('GET', '/api/v1/documents/' + artifactId + '/file?for_upload=true', undefined, {raw: true});
      if (!response.ok) {
        let detail = 'HTTP ' + response.status;
        try { detail = (await response.json()).detail || detail; } catch (error) { /* keep */ }
        throw new ApiError(response.status, detail);
      }
      const buffer = await response.arrayBuffer();
      return {bytes: new Uint8Array(buffer), sha256: response.headers.get('X-Content-SHA256') || null, mime: response.headers.get('Content-Type') || 'application/octet-stream', disposition: response.headers.get('Content-Disposition') || ''};
    }
  }

  async function sha256Hex(bytes) {
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    return Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, '0')).join('');
  }

  function toBase64(bytes) {
    let binary = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    return btoa(binary);
  }

  root.CareerOS = {DEFAULTS, ApiError, CareerOSApi, loadSettings, saveSettings, isLocalUrl, sha256Hex, toBase64};
})(globalThis);
