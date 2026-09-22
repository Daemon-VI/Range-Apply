// Popup: shows what the extension knows about the current tab and drives
// the steps a person triggers (fill, submit, answer, pause, confirm).
(function () {
  const $ = (id) => document.getElementById(id);
  let tabId = null;

  function send(message) {
    return new Promise((resolve) => chrome.runtime.sendMessage(Object.assign({tabId: tabId}, message), (reply) => resolve(chrome.runtime.lastError ? {ok: false, error: chrome.runtime.lastError.message} : reply)));
  }

  const PHASES = {
    matched: ['This page matches a queued application.', ''],
    claimed: ['Claimed; preparing…', ''],
    filled: ['Filled. Review the page, then press the site\'s submit button (or Submit now). The server checks caps, cool-downs and duplicates right before it goes.', 'ok'],
    awaiting_user: ['Filled what could be answered truthfully. The remaining fields need you.', 'warn'],
    submitted: ['Submitted; waiting for the confirmation page…', ''],
    settled: ['Done.', 'ok'],
    handoff: ['Handed to you.', 'bad'],
    error: ['Something went wrong.', 'bad'],
  };

  function render(job, url) {
    $('message').hidden = true;
    $('match').hidden = !job;
    $('none').hidden = !!job;
    if (!job) return;
    $('job-title').textContent = job.title || '';
    $('job-company').textContent = (job.company || '') + (job.applicationId ? ' · attempt ' + job.applicationId.slice(0, 8) : '');
    const [text, cls] = PHASES[job.phase] || ['', ''];
    const phase = $('phase');
    phase.className = 'phase ' + cls;
    phase.textContent = text + (job.outcome ? ' Outcome: ' + job.outcome + (job.attemptStatus ? ' (attempt ' + job.attemptStatus + ')' : '') : '');
    $('note').textContent = job.note || '';
    $('btn-fill').hidden = !['matched', 'awaiting_user', 'error', 'handoff'].includes(job.phase);
    $('btn-fill').textContent = job.phase === 'matched' ? 'Fill this application' : 'Fill again';
    $('btn-submit').hidden = job.phase !== 'filled';
    $('btn-pause').hidden = !['filled', 'awaiting_user'].includes(job.phase);
    const pending = $('pending');
    pending.hidden = job.phase !== 'awaiting_user';
    if (!pending.hidden) {
      const list = $('pending-list');
      list.innerHTML = '';
      for (const field of (job.pending || [])) {
        const box = document.createElement('div');
        box.className = 'pending';
        const label = document.createElement('div'); label.textContent = field.label + (field.required ? ' *' : '');
        const why = document.createElement('div'); why.className = 'why'; why.textContent = field.reason || field.status;
        const input = document.createElement('input'); input.placeholder = 'your truthful answer';
        const save = document.createElement('label'); const cb = document.createElement('input'); cb.type = 'checkbox'; save.appendChild(cb); save.appendChild(document.createTextNode(' save to my answer bank'));
        const btn = document.createElement('button'); btn.textContent = 'Use this answer';
        btn.addEventListener('click', async () => {
          if (!input.value.trim()) return;
          btn.disabled = true;
          const reply = await send({type: 'popup:answer', field_id: field.field_id, answer: input.value.trim(), save_to_bank: cb.checked});
          if (!reply.ok) { $('note').textContent = reply.error; btn.disabled = false; return; }
          render(reply.job, url);
        });
        box.append(label, why, input, save, btn);
        list.appendChild(box);
      }
      for (const label of (job.missing || [])) {
        if ((job.pending || []).some(p => p.label === label)) continue;
        const box = document.createElement('div'); box.className = 'pending'; box.textContent = label + ' — could not be set on the page; fill it there.'; list.appendChild(box);
      }
    }
    const handoff = $('handoff');
    handoff.hidden = job.phase !== 'handoff';
    if (!handoff.hidden) $('handoff-text').textContent = (job.handoffReason || '') + ': ' + (job.handoffMessage || '') + '. Finish on the page yourself, then tell CareerOS what happened.';
  }

  async function refresh() {
    const status = await send({type: 'popup:status'});
    const conn = $('conn');
    if (!status.ok) {
      conn.textContent = 'not connected'; conn.className = 'pill bad';
      $('message').hidden = true; $('setup').hidden = false; $('match').hidden = true; $('none').hidden = true;
      $('setup').querySelector('p').textContent = status.error || 'Not connected.';
      return;
    }
    conn.textContent = status.settings.submitMode + ' · ' + status.status.ready + ' ready'; conn.className = 'pill ok';
    $('queue-summary').textContent = status.status.pending_items + ' queued submit item(s); open one of the application pages from the CareerOS execution dashboard.';
    const state = await send({type: 'popup:state'});
    if (!state.ok) { $('message').textContent = state.error; return; }
    render(state.job, state.url);
  }

  async function act(type, extra) {
    const buttons = document.querySelectorAll('button');
    buttons.forEach(b => { b.disabled = true; });
    $('note').textContent = 'working…';
    const reply = await send(Object.assign({type: type}, extra || {}));
    buttons.forEach(b => { b.disabled = false; });
    if (!reply.ok) { $('note').textContent = reply.error || 'failed'; await refresh(); return; }
    await refresh();
  }

  $('btn-fill').addEventListener('click', () => act('popup:fill'));
  $('btn-submit').addEventListener('click', () => act('popup:submit'));
  $('btn-pause').addEventListener('click', () => act('popup:pause', {reason: 'USER_CONFIRMATION_REQUIRED', message: 'paused by the candidate'}));
  $('btn-confirm-yes').addEventListener('click', () => act('popup:confirm', {submitted: true, reference: $('confirm-ref').value.trim() || null}));
  $('btn-confirm-no').addEventListener('click', () => act('popup:confirm', {submitted: false}));
  $('reset').addEventListener('click', (e) => { e.preventDefault(); act('popup:reset'); });
  $('open-options').addEventListener('click', (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); });
  $('open-dashboard').addEventListener('click', async (e) => { e.preventDefault(); const s = await CareerOS.loadSettings(); chrome.tabs.create({url: s.baseUrl + '/dashboard/execution'}); });

  chrome.tabs.query({active: true, currentWindow: true}, (tabs) => { tabId = tabs[0] && tabs[0].id; refresh(); });
})();
