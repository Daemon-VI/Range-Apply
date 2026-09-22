// Shared behaviour for the local application-form fixtures.
// Every fixture is a static page; "submitting" is a JS-only transition so no
// network is ever involved.
(function () {
  function qs(name) {
    return new URLSearchParams(location.search).get(name);
  }
  window.fixture = {
    // Navigate to a confirmation page carrying the reference id.
    confirm: function (ref, path) {
      const target = path || 'confirmation.html';
      location.href = target + '?ref=' + encodeURIComponent(ref || 'REF-' + Date.now());
    },
    // Show an inline validation error and stay on the page.
    reject: function (message) {
      let box = document.getElementById('form-errors');
      if (!box) {
        box = document.createElement('div');
        box.id = 'form-errors';
        box.setAttribute('role', 'alert');
        box.className = 'error';
        document.querySelector('form').prepend(box);
      }
      box.textContent = message || 'Please fill in all required fields';
    },
    // Reveal a CAPTCHA widget (simulated) and stay on the page.
    challenge: function () {
      const box = document.createElement('div');
      box.className = 'g-recaptcha';
      box.setAttribute('data-sitekey', 'fixture');
      box.textContent = 'Verify that you are human';
      document.querySelector('form').prepend(box);
    },
    // Do nothing at all: an ambiguous post-submit state.
    hang: function () {},
    // Report whether required inputs are empty (like a browser would).
    missingRequired: function (form) {
      const missing = [];
      form.querySelectorAll('[required]').forEach(function (el) {
        if (el.type === 'radio') {
          if (!form.querySelector('input[name="' + el.name + '"]:checked')) missing.push(el.name);
        } else if (el.type === 'checkbox') {
          if (!el.checked) missing.push(el.name);
        } else if (el.type === 'file') {
          if (!el.files || !el.files.length) missing.push(el.name);
        } else if (!el.value) missing.push(el.name);
      });
      return Array.from(new Set(missing));
    },
    param: qs,
  };
})();
