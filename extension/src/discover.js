// Form discovery shared by the local Playwright executor and the browser
// extension: one script, one canonical FormSnapshot shape.
//
// Reports structure only (labels, types, required, options, stable
// selectors), plus the conditions that must stop automation: CAPTCHA /
// human verification, login walls, MFA prompts, custom widgets. Never
// values typed into password fields, never page HTML.
globalThis.careerosDiscover = function careerosDiscover() {
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/["\\]/g, '\\$&');
  const text = (el) => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const isVisible = (el) => {
    if (!el) return false;
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0 || el.type === 'file';
  };
  // Text that lives beside a control but is not its question: hints, errors,
  // typeahead results, upload status (real Lever pages wrap all of it in the
  // same <label> as the caption). A caption element wins when there is one.
  const NOISE = '[aria-hidden="true"],[role="alert"],[role="status"],[aria-live],[hidden],svg,[class*="error"],[class*="hint"],[class*="help"],[class*="dropdown"],[class*="no-results"],[class*="status"],[class*="filename"],[class*="upload"],[class*="description"]';
  const CAPTION = '.application-label, legend, [class*="label"]:not(label)';
  const stripControls = (node) => {
    const cap = Array.from(node.querySelectorAll(CAPTION)).find(c => !c.querySelector('input,select,textarea'));
    if (cap) { const t = text(cap); if (t) return t; }
    const clone = node.cloneNode(true);
    clone.querySelectorAll('input,select,textarea,button,option,' + NOISE).forEach(n => n.remove());
    return text(clone);
  };
  // A question title that is a <label for="..."> naming no element (real Ashby
  // pages: the location combobox, yes/no questions and radio groups carry no id
  // matching their title), found within a few ancestor levels of the control.
  // Climbing stops at the first ancestor that also holds another control: a title
  // beyond that belongs to a neighbouring question (the real Ashby autofill upload
  // picked up the required Location title that way).
  const danglingTitle = (el, maxLevels) => {
    let node = el.parentElement;
    for (let i = 0; node && i < maxLevels && node !== document.body; i++) {
      const others = Array.from(node.querySelectorAll('input, select, textarea')).filter(c => c !== el && !(el.name && c.name === el.name) && c.type !== 'hidden' && c.getAttribute('aria-hidden') !== 'true');
      if (others.length) return null;
      const found = Array.from(node.querySelectorAll('label[for]')).find(l => !l.contains(el) && !l.querySelector('input,select,textarea') && !document.getElementById(l.htmlFor));
      if (found) return found;
      if (node.tagName === 'FORM') break;
      node = node.parentElement;
    }
    return null;
  };
  const nearestHeading = (el, maxLevels) => {
    let node = el.parentElement;
    for (let i = 0; node && i < maxLevels && node !== document.body; i++) {
      const candidates = Array.from(node.querySelectorAll('legend, .label, [class*="label"]:not(label), h1, h2, h3, h4, p, span.question, div.question'));
      for (const c of candidates) {
        if (c === el || c.contains(el)) continue;
        if (c.querySelector('input,select,textarea')) continue;
        const t = text(c); if (t && t.length <= 300) return t;
      }
      const own = Array.from(node.querySelectorAll('label')).filter(l => !l.contains(el) && !l.querySelector('input,select,textarea') && !l.htmlFor);
      if (own.length === 1) { const t = text(own[0]); if (t) return t; }
      if (node.tagName === 'FORM') break;
      node = node.parentElement;
    }
    return '';
  };
  const labelOf = (el) => {
    if (el.labels && el.labels.length) { const t = Array.from(el.labels).map(stripControls).join(' ').trim(); if (t) return t; }
    const aria = el.getAttribute('aria-label'); if (aria && aria.trim()) return aria.trim();
    const by = el.getAttribute('aria-labelledby');
    if (by) { const t = by.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(text).join(' ').trim(); if (t) return t; }
    const wrap = el.closest('label'); if (wrap) { const t = stripControls(wrap); if (t) return t; }
    const title = danglingTitle(el, 2); if (title) { const t = text(title); if (t) return t; }
    const near = nearestHeading(el, 4); if (near) return near;
    return el.getAttribute('placeholder') || el.getAttribute('title') || el.name || el.id || '';
  };
  // What a searchable dropdown currently shows as its choice (react-select keeps
  // it in a "single-value" element next to the input, not in the input's value).
  const comboboxValue = (el) => {
    const box = el.closest('[class*="control"]') || el.closest('[class*="container"]') || el.parentElement;
    const shown = box ? box.querySelector('[class*="single-value"], [class*="singleValue"], [class*="selected-value"], [class*="selectedValue"]') : null;
    const t = shown ? text(shown) : '';
    return t || el.value || null;
  };
  const selectorFor = (el) => {
    if (el.id && document.querySelectorAll('#' + esc(el.id)).length === 1) return '#' + esc(el.id);
    if (el.name) {
      const tag = el.tagName.toLowerCase();
      const sel = tag + '[name="' + esc(el.name) + '"]';
      if (document.querySelectorAll(sel).length === 1) return sel;
      if (el.type === 'radio' || el.type === 'checkbox') return sel + '[value="' + esc(el.value) + '"]';
      return sel;
    }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 6) {
      const tag = node.tagName.toLowerCase();
      const sib = Array.from(node.parentNode ? node.parentNode.children : []).filter(c => c.tagName === node.tagName);
      parts.unshift(sib.length > 1 ? tag + ':nth-of-type(' + (sib.indexOf(node) + 1) + ')' : tag);
      node = node.parentNode;
      if (node === document.body) break;
    }
    return parts.join(' > ');
  };
  // Required is also said by the title's own class (Ashby: "_required_…") or a
  // trailing "✱" (Lever marks the resume that way without the attribute).
  const marksRequired = (l) => !!l && /(^|\s)_?required(_|\s|$)/i.test(String(l.className || ''));
  const isRequired = (el, label) => el.required || el.getAttribute('aria-required') === 'true' || /[*\u2731]\s*$/.test(label || '') || /\(required\)/i.test(label || '') || !!el.closest('[data-required="true"], .required') || Array.from(el.labels || []).some(marksRequired) || marksRequired(danglingTitle(el, 3));
  const controls = Array.from(document.querySelectorAll('input, select, textarea'));
  const fields = [];
  const groups = {};
  for (const el of controls) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'input' ? 'text' : tag)).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type)) continue;
    // react-select and similar widgets keep an aria-hidden, unfocusable dummy input next to the real combobox: not a field.
    if (el.getAttribute('aria-hidden') === 'true' || (el.closest('[aria-hidden="true"]') && el.getAttribute('tabindex') === '-1')) continue;
    if (type === 'password') { fields.push({kind: 'password', label: labelOf(el), selector: selectorFor(el), visible: isVisible(el)}); continue; }
    if (el.disabled) continue;
    // Yes / No answered by two buttons over a hidden checkbox (real Ashby pages).
    // The executor presses the button whose text matches the answer (2026-09-22);
    // the buttons' texts are the options, the pressed one the current value.
    if (type === 'checkbox' && !isVisible(el) && el.parentElement && el.parentElement.querySelector('button[aria-pressed]')) {
      const title = danglingTitle(el, 2);
      const ylabel = title ? text(title) : nearestHeading(el, 3);
      const buttons = Array.from(el.parentElement.querySelectorAll('button[aria-pressed]'));
      const pressed = buttons.find(b => b.getAttribute('aria-pressed') === 'true');
      fields.push({external_id: el.name || el.id || null, label: ylabel, field_type: 'yesno', required: isRequired(el, ylabel), options: buttons.map(b => ({label: text(b), value: text(b)})).filter(o => o.label), selector: selectorFor(el), input_type: 'yesno', current_value: pressed ? text(pressed) : null, accept: null, visible: false});
      continue;
    }
    if (!isVisible(el) && type !== 'file') continue;
    if (type === 'radio' || (type === 'checkbox' && el.name && controls.filter(c => c.name === el.name && c.type === 'checkbox').length > 1)) {
      const key = type + ':' + (el.name || selectorFor(el));
      if (!groups[key]) {
        const box = el.closest('fieldset, [role="group"], [role="radiogroup"], .application-question, .field, .form-group, [class*="question"]');
        let glabel = '';
        if (box) { const lg = Array.from(box.querySelectorAll('legend, .label, [class*="label"]:not(label), h3, h4, p')).find(c => !c.contains(el) && !c.querySelector('input,select,textarea')); glabel = lg ? text(lg) : ''; }
        if (!glabel) { const title = danglingTitle(el, 3); glabel = title ? text(title) : ''; }
        if (!glabel) glabel = nearestHeading(el, 5);
        const groupRequired = isRequired(el, glabel) || (box && !!box.querySelector('input[name="' + esc(el.name) + '"][required], input[name="' + esc(el.name) + '"][aria-required="true"]'));
        groups[key] = {external_id: el.name || null, label: glabel || el.name || '', field_type: type === 'radio' ? 'radio' : 'multi_select', required: groupRequired, options: [], selector: tag + '[name="' + esc(el.name) + '"]', input_type: type, current_value: null};
        fields.push(groups[key]);
      }
      const optLabel = (el.labels && el.labels.length) ? text(el.labels[0]) : (el.closest('label') ? text(el.closest('label')) : (el.value || ''));
      groups[key].options.push({label: optLabel || el.value, value: el.value});
      if (el.checked) groups[key].current_value = el.value;
      continue;
    }
    const label = labelOf(el);
    let ftype = 'unknown';
    let options = [];
    // A searchable dropdown (Greenhouse react-select, Ashby location): its options
    // only render once the person types, so the executor types the answer and
    // picks the option that matches it on the page (2026-09-22). Options already
    // rendered (an open list) are reported; otherwise the list is empty here.
    const combobox = tag === 'input' && (el.getAttribute('role') || '').toLowerCase() === 'combobox';
    if (combobox) {
      ftype = 'combobox';
      const listId = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
      const list = listId ? document.getElementById(listId) : null;
      options = list ? Array.from(list.querySelectorAll('[role="option"]')).map(o => ({label: text(o), value: text(o)})).filter(o => o.label) : [];
    } else if (tag === 'select') {
      ftype = el.multiple ? 'multi_select' : 'select';
      options = Array.from(el.options).filter(o => o.value !== '' || (o.textContent || '').trim() !== '').filter(o => !/^(select|choose|please select|--)/i.test((o.textContent || '').trim())).map(o => ({label: (o.textContent || '').trim(), value: o.value}));
    } else if (tag === 'textarea') ftype = 'textarea';
    else if (type === 'text') ftype = 'text';
    else if (type === 'email') ftype = 'email';
    else if (type === 'tel') ftype = 'phone';
    else if (type === 'number') ftype = 'numeric';
    else if (type === 'date' || type === 'month') ftype = 'date';
    else if (type === 'file') ftype = 'file';
    else if (type === 'checkbox') ftype = 'checkbox';
    else if (type === 'url') ftype = 'text';
    else ftype = 'unknown';
    const options2 = (ftype === 'checkbox') ? [{label: label, value: el.value || 'on'}] : options;
    fields.push({external_id: el.name || el.id || null, label: label, field_type: ftype, required: isRequired(el, label), options: options2, selector: selectorFor(el), input_type: combobox ? 'combobox' : type, current_value: (ftype === 'file' || type === 'password') ? null : (combobox ? comboboxValue(el) : (el.value || null)), accept: el.getAttribute('accept') || null, visible: isVisible(el)});
  }
  // A listing page often holds only an "Apply" link / button that leads to the
  // form (Stripe, employer career sites in front of Greenhouse, 2026-09-22).
  // Reported so the executor can follow it once; following a link is navigation,
  // never a submission.
  const APPLY_TEXT = /^\s*apply(\s+(now|here|today|online|for this (job|role|position)|to this (job|role|position)))?\s*$/i;
  const apply_links = Array.from(document.querySelectorAll('a[href], button')).filter(isVisible).filter(el => APPLY_TEXT.test(text(el) || el.getAttribute('aria-label') || '')).slice(0, 5).map(el => ({text: text(el), href: el.getAttribute('href') || null, selector: selectorFor(el)}));
  const custom = Array.from(document.querySelectorAll('[role="combobox"], [role="listbox"], [contenteditable="true"], [data-widget], .Select-control, .react-select__control')).filter(isVisible).filter(el => !el.matches('input, select, textarea'));
  const bodyText = text(document.body).slice(0, 20000);
  const captchaEls = Array.from(document.querySelectorAll('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], iframe[src*="captcha"], .g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], #captcha, [id*="captcha"], [class*="captcha"]'));
  const captchaText = /verify (that )?you('re| are) (a )?human|are you a robot|i am not a robot|complete the security check|bot check|human verification|unusual traffic/i.test(bodyText);
  // An *invisible* reCAPTCHA (the corner badge; the provider scores the submit
  // itself and only then may show a challenge) is not something a person can
  // complete up front. It is reported, but only a visible widget or challenge
  // text is a wall. Whatever appears after the click is handled after the click.
  const captchaInvisible = (el) => el.classList.contains('grecaptcha-badge') || (el.closest && el.closest('.grecaptcha-badge')) || /size=invisible/.test(el.getAttribute('src') || '') || (el.getAttribute('data-size') || '').toLowerCase() === 'invisible' || !isVisible(el) || el.getBoundingClientRect().width < 40 || el.getBoundingClientRect().height < 40;
  const captcha_visible = captchaText || captchaEls.some(el => !captchaInvisible(el));
  const captcha = captchaText || captchaEls.length > 0;
  // A widget inside the application form itself (Lever's hCaptcha, Greenhouse's
  // reCAPTCHA) is not a wall: the person completes it and the run may continue.
  // Solving is only ever detected (the provider's response token), never done.
  const captchaWidget = document.querySelector('.g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey], iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"]');
  const captchaForm = captchaWidget ? captchaWidget.closest('form') : null;
  const captcha_inline = !!(captchaForm && captchaForm.querySelector('input:not([type="hidden"]), textarea, select'));
  const captcha_solved = Array.from(document.querySelectorAll('[name="g-recaptcha-response"], [name="h-captcha-response"], [name="cf-turnstile-response"]')).some(el => (el.value || '').length > 0);
  const mfa = !!document.querySelector('input[autocomplete="one-time-code"], input[name*="otp" i], input[name*="mfa" i], input[id*="otp" i]') || /verification code|one-time (code|password)|two-factor|2fa|authenticator app|enter the code we sent/i.test(bodyText);
  const passwords = fields.filter(f => f.kind === 'password' && f.visible);
  const submitCandidates = Array.from(document.querySelectorAll('button, input[type="submit"]')).filter(isVisible).map(b => ({text: text(b) || b.value || '', selector: selectorFor(b), type: (b.getAttribute('type') || '').toLowerCase()}));
  const errors = Array.from(document.querySelectorAll('[role="alert"], .error, .field-error, .error-message, .form-error, [class*="error"], [aria-invalid="true"]')).filter(isVisible).map(text).filter(Boolean).slice(0, 10);
  return {url: location.href, title: document.title, fields: fields.filter(f => !f.kind), custom_widgets: custom.length, apply_links: apply_links, captcha: captcha, captcha_visible: captcha_visible, captcha_inline: captcha_inline, captcha_solved: captcha_solved, mfa: mfa, login_wall: passwords.length > 0, submit_buttons: submitCandidates, errors: errors, body_excerpt: bodyText.slice(0, 2000)};
};
