(function () {
  const $ = (id) => document.getElementById(id);
  const fields = ['baseUrl', 'apiKey', 'submitMode', 'submitWaitMs'];

  async function load() {
    const s = await CareerOS.loadSettings();
    for (const f of fields) $(f).value = s[f];
    const granted = await chrome.permissions.contains({origins: ['https://*/*', 'http://*/*']});
    $('grant-state').textContent = granted ? 'Granted for all sites.' : 'Not granted: the extension only sees a page after you open the popup on it (activeTab).';
    $('grant').disabled = granted;
  }

  async function save() {
    const patch = {};
    for (const f of fields) patch[f] = f === 'submitWaitMs' ? Number($(f).value) || 15000 : $(f).value.trim();
    if (!CareerOS.isLocalUrl(patch.baseUrl)) { $('result').textContent = 'The server address must be http://127.0.0.1:PORT or http://localhost:PORT.'; return false; }
    await CareerOS.saveSettings(patch);
    $('result').textContent = 'Saved.';
    return true;
  }

  $('save').addEventListener('click', save);
  $('test').addEventListener('click', async () => {
    if (!(await save())) return;
    try {
      const client = new CareerOS.CareerOSApi(await CareerOS.loadSettings());
      const status = await client.status();
      $('result').textContent = 'Connected: tenant ' + status.tenant_id + ', ' + status.ready + ' attempt(s) ready, ' + status.pending_items + ' queued submit item(s).';
    } catch (error) {
      $('result').textContent = 'Connection failed: ' + error.message + (error.status === 401 ? ' (check the API key)' : '');
    }
  });
  $('grant').addEventListener('click', async () => {
    await chrome.permissions.request({origins: ['https://*/*', 'http://*/*']});
    load();
  });
  load();
})();
