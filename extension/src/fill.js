// Filling shared logic for the browser extension: applies the server's
// mapped answers to the discovered fields. Nothing is guessed here: a field
// without an ANSWERED mapping is left alone (and reported as missing when
// it is required). File fields receive the preparation's rendered document
// bytes fetched from the local CareerOS API.
globalThis.careerosFill = function careerosFill(answers, artifacts) {
  const setNative = (el, value) => {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : el.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(el, value); else el.value = value;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const check = (el) => { if (!el.checked) { el.click(); if (!el.checked) { el.checked = true; el.dispatchEvent(new Event('change', {bubbles: true})); } } };
  const bytesOf = (b64) => { const bin = atob(b64); const out = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i); return out; };
  const report = {filled: [], skipped: [], missing_required: [], file_missing: []};
  for (const answer of answers) {
    const selector = answer.selector;
    if (!selector) { if (answer.required && answer.status === 'ANSWERED') report.missing_required.push(answer.label); continue; }
    if (answer.status !== 'ANSWERED') { (answer.required ? report.missing_required : report.skipped).push(answer.label); continue; }
    const el = document.querySelector(selector);
    if (!el) { (answer.required ? report.missing_required : report.skipped).push(answer.label); continue; }
    try {
      switch (answer.field_type) {
        case 'text': case 'textarea': case 'email': case 'phone': case 'numeric': case 'date':
          if (answer.answer == null) { report.skipped.push(answer.label); break; }
          setNative(el, String(answer.answer)); report.filled.push(answer.label); break;
        case 'select': {
          const value = (answer.selected_values || [])[0];
          if (value == null) { report.skipped.push(answer.label); break; }
          const option = Array.from(el.options).find(o => o.value === value || o.textContent.trim() === value);
          if (!option) { (answer.required ? report.missing_required : report.skipped).push(answer.label); break; }
          setNative(el, option.value); report.filled.push(answer.label); break;
        }
        case 'multi_select': {
          const values = answer.selected_values || [];
          if (!values.length) { report.skipped.push(answer.label); break; }
          if (el.tagName === 'SELECT') { Array.from(el.options).forEach(o => { o.selected = values.includes(o.value) || values.includes(o.textContent.trim()); }); el.dispatchEvent(new Event('change', {bubbles: true})); }
          else { values.forEach(v => { const box = document.querySelector(selector + '[value="' + CSS.escape(v) + '"]'); if (box) check(box); }); }
          report.filled.push(answer.label); break;
        }
        case 'radio': {
          const value = (answer.selected_values || [])[0];
          if (value == null) { report.skipped.push(answer.label); break; }
          const radio = document.querySelector(selector + '[value="' + CSS.escape(value) + '"]');
          if (!radio) { (answer.required ? report.missing_required : report.skipped).push(answer.label); break; }
          check(radio); report.filled.push(answer.label); break;
        }
        case 'checkbox':
          if (!(answer.selected_values || []).length) { report.skipped.push(answer.label); break; }
          check(el); report.filled.push(answer.label); break;
        case 'file': {
          const artifact = answer.artifact_type ? (artifacts || {})[answer.artifact_type] : null;
          if (!artifact || !artifact.base64) { (answer.required ? report.file_missing : report.skipped).push(answer.label); break; }
          const file = new File([bytesOf(artifact.base64)], artifact.name || 'document.pdf', {type: artifact.mime || 'application/pdf'});
          const transfer = new DataTransfer();
          transfer.items.add(file);
          el.files = transfer.files;
          el.dispatchEvent(new Event('change', {bubbles: true}));
          report.filled.push(answer.label); break;
        }
        default:
          (answer.required ? report.missing_required : report.skipped).push(answer.label);
      }
    } catch (error) {
      (answer.required ? report.missing_required : report.skipped).push(answer.label);
    }
  }
  return report;
};

globalThis.careerosFindSubmit = function careerosFindSubmit(locators) {
  for (const selector of (locators || [])) {
    try {
      const el = document.querySelector(selector);
      if (el && el.offsetParent !== null) return el;
    } catch (error) { /* invalid selector for this page: try the next */ }
  }
  const buttons = Array.from(document.querySelectorAll('button[type="submit"], input[type="submit"], button')).filter(b => b.offsetParent !== null);
  return buttons.find(b => /submit|apply|send/i.test((b.textContent || b.value || ''))) || buttons.find(b => (b.getAttribute('type') || '').toLowerCase() === 'submit') || null;
};
