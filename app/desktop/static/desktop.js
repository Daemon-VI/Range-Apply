/* CareerOS desktop control center — the only script the pages need.
 *
 * Writes go to the EXISTING API routes with the dashboard cookie (sent by the
 * browser, same origin) plus the mandatory desktop header. No key is ever
 * embedded here. After a write, panels marked hx-trigger="careeros:refresh
 * from:body" reload themselves through htmx; the result is shown as a toast
 * in an aria-live region.
 */
(function () {
  'use strict';
  var HEADER = {'X-Requested-With': 'careeros-desktop'};

  function toast(text, kind) {
    var host = document.getElementById('toasts');
    if (!host) return;
    var el = document.createElement('div');
    el.className = 'toast toast-' + (kind || 'info');
    el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    el.textContent = text;
    host.appendChild(el);
    setTimeout(function () { el.classList.add('toast-hide'); }, kind === 'error' ? 6500 : 3500);
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, kind === 'error' ? 7200 : 4200);
  }

  function describe(status, body) {
    if (body && typeof body === 'object') {
      if (body.detail) return typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail).slice(0, 200);
      if (body.message) return String(body.message).slice(0, 200);
      if (body.error) return String(body.error).slice(0, 200);
    }
    return 'HTTP ' + status;
  }

  /* data-require-input="<input id>" + data-require-value="SUBMIT": the click
   * does nothing at all unless the person typed that exact phrase. The guard
   * is a deliberate speed bump in front of a real submission, never a
   * credential. */
  function phraseTyped(el) {
    var inputId = el.getAttribute('data-require-input');
    var required = el.getAttribute('data-require-value');
    if (!inputId || !required) return true;
    var input = document.getElementById(inputId);
    var typed = input ? String(input.value || '') : '';
    if (typed === required) return true;
    toast('Type ' + required + ' to confirm', 'error');
    if (input) input.focus();
    return false;
  }

  // careeros.action(button) — reads data-method, data-url, data-body (JSON),
  // data-form, data-confirm, data-done, data-next, data-next-from and the
  // typed-phrase guard data-require-input / data-require-value.
  function action(el) {
    var method = (el.getAttribute('data-method') || 'POST').toUpperCase();
    var url = el.getAttribute('data-url');
    var confirmText = el.getAttribute('data-confirm');
    var done = el.getAttribute('data-done') || 'Done';
    var body = el.getAttribute('data-body');
    var form = el.getAttribute('data-form');
    if (!url) return;
    if (!phraseTyped(el)) return;
    if (confirmText && !window.confirm(confirmText)) return;
    var payload = null;
    if (form) {
      var f = document.getElementById(form);
      if (f) {
        payload = {};
        Array.prototype.forEach.call(f.elements, function (input) {
          if (!input.name) return;
          if (input.type === 'checkbox') payload[input.name] = input.checked;
          else payload[input.name] = input.value;
        });
      }
    } else if (body) {
      try { payload = JSON.parse(body); } catch (e) { payload = null; }
    }
    el.disabled = true;
    el.classList.add('busy');
    el.setAttribute('aria-busy', 'true');
    var init = {method: method, headers: Object.assign({'Accept': 'application/json'}, HEADER), credentials: 'same-origin'};
    if (payload !== null && method !== 'GET') {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(payload);
    }
    fetch(url, init).then(function (res) {
      return res.text().then(function (text) {
        var parsed = null;
        try { parsed = text ? JSON.parse(text) : null; } catch (e) { parsed = null; }
        if (res.ok) {
          toast(done, 'ok');
          document.body.dispatchEvent(new CustomEvent('careeros:refresh', {bubbles: true}));
          var nextFrom = el.getAttribute('data-next-from');
          var next = el.getAttribute('data-next');
          if (nextFrom && parsed && typeof parsed === 'object' && parsed[nextFrom]) window.location.href = parsed[nextFrom];
          else if (next) window.location.href = next;
        } else {
          toast('Not applied: ' + describe(res.status, parsed), 'error');
        }
      });
    }).catch(function (err) {
      toast('Request failed: ' + (err && err.message ? err.message : err), 'error');
    }).then(function () {
      el.disabled = false;
      el.classList.remove('busy');
      el.removeAttribute('aria-busy');
    });
  }

  /* data-reveal: show / hide an abbreviated value (email, phone) in place.
   * The full value is the candidate's own data on their own machine; it is
   * abbreviated only so it does not sit in screenshots by default. */
  function reveal(button) {
    var host = button.closest('.masked-value');
    if (!host) return;
    var full = host.querySelector('[data-full]');
    var masked = host.querySelector('[data-masked]');
    if (!full || !masked) return;
    var show = full.hidden;
    full.hidden = !show;
    masked.hidden = show;
    button.textContent = show ? 'Hide' : 'Show';
    button.setAttribute('aria-expanded', show ? 'true' : 'false');
  }

  document.addEventListener('click', function (ev) {
    var revealEl = ev.target.closest('[data-reveal]');
    if (revealEl) {
      ev.preventDefault();
      reveal(revealEl);
      return;
    }
    var el = ev.target.closest('[data-action]');
    if (!el) return;
    ev.preventDefault();
    action(el);
  });

  // htmx partial loads: show a loading state and surface failures.
  document.addEventListener('htmx:beforeRequest', function (ev) {
    var target = ev.detail && ev.detail.target;
    if (target && target.classList) {
      target.classList.add('loading');
      target.setAttribute('aria-busy', 'true');
    }
  });
  document.addEventListener('htmx:afterRequest', function (ev) {
    var target = ev.detail && ev.detail.target;
    if (target && target.classList) {
      target.classList.remove('loading');
      if (target.id !== 'run-status') target.removeAttribute('aria-busy');
    }
  });
  document.addEventListener('htmx:responseError', function (ev) {
    var xhr = ev.detail && ev.detail.xhr;
    toast('Could not load panel: HTTP ' + (xhr ? xhr.status : '?'), 'error');
  });
  document.addEventListener('htmx:sendError', function () {
    toast('Could not reach CareerOS — is the app still running?', 'error');
  });

  window.careeros = {action: action, toast: toast};
})();
