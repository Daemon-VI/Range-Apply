# CareerOS Apply – browser extension (Blueprint Phase 9)

The second execution mechanism next to the local Playwright worker. It runs
in your own Chromium-based browser (Chrome, Edge, Brave …), against the
application pages you open yourself, and drives the **same Phase 6 execution
API** an external executor uses. Nothing here decides what to answer: the
server maps every field from the READY preparation, the profile and the
answer bank; the extension only puts those answers on the page.

```
READY attempt ──scheduler──▶ SUBMIT queue item
      │  you open the application page
      ▼
/extension/match   badge "1": this page is a queued application
/extension/claim   guarded claim (one worker at a time)
/items/{id}/start  idempotency · preconditions · documents · ExecutionPackage
discover.js        structure only → /items/{id}/form → server maps answers
/documents/{id}/file?for_upload=true   rendered PDF/DOCX, SHA-256 re-checked
fill.js            fills safe fields, attaches documents
   (pauses for you: NEEDS_USER_INPUT fields, CAPTCHA, login, MFA, no document)
submit             your click or the extension's → intercepted →
/extension/gate    caps · cool-down · blocklist · duplicates · kill switch · staleness
                   → submit_invoked recorded → the click proceeds
observe            confirmation text / reference / validation errors / nothing
/items/{id}/result server verifies (VERIFIED / LIKELY / UNCERTAIN / NEEDS_REVIEW)
```

## Install (unpacked)

1. Start CareerOS locally (`uvicorn app.main:app`) with `API_KEY` set in `.env`.
2. `chrome://extensions` → enable *Developer mode* → *Load unpacked* → pick
   this `extension/` folder.
3. Open the extension's **Options**: server address (`http://127.0.0.1:8000`),
   the API key, submit mode. *Test connection* must say "Connected".
4. Run the scheduler (dashboard → Scheduler → Run) so READY attempts become
   SUBMIT queue items; open one application page from *Execution → attempt →
   target*. The extension badge shows **1** on a matching tab.
5. Click the extension → **Fill this application**.

## Submit modes

| mode | what happens |
|---|---|
| `manual` (default) | The extension fills. You press the site's own submit button (or *Submit now*). The click is intercepted, the server gate runs, then the click proceeds. |
| `auto` | Same, but the extension presses submit itself once every field it is allowed to fill is filled and the gate passes. Anything unanswered → it stops and asks you. |
| `dry_run` | Fills and reports `DRY_RUN`; the submit interception is not armed. |

## What it will never do

* Invent an answer. Fields without a truthful prepared/bank/profile answer are
  left empty and listed in the popup; you answer them (optionally saving to
  your answer bank) or fill them on the page.
* Upload anything but the preparation's rendered, validated document
  (upload-eligible only; bytes re-hashed before they reach the page).
* Solve or bypass a CAPTCHA, log in, enter an MFA code: it hands off
  (`CAPTCHA_REQUIRED` / `AUTH_REQUIRED` / `MFA_REQUIRED`), you finish, then
  you confirm from the popup (*I submitted it* / *I did not submit*).
* Submit twice. Once `submit_invoked` is recorded, an unclear outcome is
  `UNCERTAIN` and only verification or your confirmation can settle it.
* Talk to anything but `http://127.0.0.1` / `http://localhost`. The API key
  lives in `chrome.storage.local` of your profile; the source carries none.

## Permissions

`storage` (settings), `tabs` (URL of the current tab for matching),
`activeTab` + `scripting` (inject `discover.js`/`fill.js`/`content.js` into the
page you clicked the popup on), `alarms` (lease heartbeat, post-submit
settlement). `host_permissions` covers only localhost. Granting the optional
"all sites" permission lets the extension inspect a confirmation page that
lives on another origin; without it such cases end as UNCERTAIN, never as a
resubmit.

## Layout

```
extension/
  manifest.json          Manifest V3
  src/api.js             local API client (also used by popup/options)
  src/background.js      service worker: match → claim → start → discover → form → fill → gate → result
  src/content.js         page bridge: discovery, fill, submit interception, evidence
  src/discover.js        form discovery (shared verbatim with the Playwright executor)
  src/fill.js            fill + submit-button lookup
  src/popup.*            per-tab status and actions
  src/options.*          server address, API key, submit mode, wait
```

Tests live in the Python suite (`tests/execution/test_extension_api.py`,
`tests/execution/test_extension_page.py`): the API flow is exercised exactly
as the service worker performs it, and the page scripts are executed under
Playwright against the local fixture forms with a stub message bridge. No
Node toolchain is required.
