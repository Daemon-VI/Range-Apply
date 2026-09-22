# Development Guide

**Last updated:** September 2026

---

## Prerequisites

- Python 3.11+ (`requires-python >= 3.11`)
- pip
- (Recommended) virtual environment
- (Optional) Docker, for a local PostgreSQL via `docker-compose.yml`

Read `docs/BLUEPRINT.md` before changing architecture; it is the source of
truth for what is being built and in which phase.

---

## Installation

```bash
# Clone/navigate to project
cd Range-Apply

# Create virtual environment (recommended)
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac

# Install with dev dependencies
pip install -e ".[dev]"
```

### Critical: migrate before you start the app

The application **never creates its own tables.** Alembic is the only
authoritative source of the schema. If you skip this step, `uvicorn` will
start, run its lifespan handler, and immediately fail with
`SchemaNotReadyError` (see `app/database.py:verify_schema`) rather than
silently creating tables that would then break every future migration.

```bash
# Required, every fresh clone / fresh database, BEFORE first run:
alembic upgrade head
```

Run it again after pulling any change that adds a migration under
`alembic/versions/`.

Optional extras, installed the same way as `dev`:

```bash
pip install -e ".[dev,postgres]"   # to run against PostgreSQL
pip install -e ".[dev,semantic]"   # to enable optional semantic skill matching
pip install -e ".[dev,llm]"        # google-genai client, if you use the Gemini provider
pip install -e ".[dev,firecrawl]"  # firecrawl-py SDK (the app also works via plain httpx)
```

---

## Environment Variables

Copy the example environment file:

```bash
copy .env.example .env
```

`.env.example` is generated from `app/config.py` and documents every setting
inline. The ones most worth knowing up front:

| Variable | Default | Notes |
|----------|---------|-------|
| `APP_ENV` | `development` | `development`/`dev`/`test` relax the `API_KEY` requirement (see below). Anything else is treated as a real deployment. |
| `DEBUG` | `false` | Leaks internals into error responses and echoes SQL when on. Never enable outside local debugging. |
| `API_KEY` | unset | Guards every write endpoint (`X-API-Key` header) **and** the dashboard. Unset is tolerated **only** when `APP_ENV` is development/dev/test — a fresh clone runs with zero configuration, with a loud one-time warning logged. Outside those environments, an unset key makes every write endpoint return `503` and the dashboard unreachable. |
| `DATABASE_URL` | `sqlite:///./careeros.db` | SQLite by default; point at PostgreSQL (`pip install -e ".[postgres]"`) for a production-shaped run. |
| `AI_ENABLED` | `false` | The global AI switch (Blueprint Phase 8b). Off: every caller uses its deterministic path and the pipeline makes zero AI calls. On: still nothing runs for a candidate until their policy's `ai_settings.enabled` is on (job-side extraction follows the global switch alone). |
| `AI_PROVIDER` | `stub` | `stub` (offline), `gemini` (needs `GEMINI_API_KEY`), `ollama` (local server, `AI_OLLAMA_URL` / `AI_OLLAMA_MODEL`). `LLM_PROVIDER=gemini` is honoured as a legacy alias for the provider choice only. |
| `AI_MAX_CALLS_PER_RUN` / `AI_TIMEOUT_SECONDS` / `AI_MAX_OUTPUT_TOKENS` | `50` / `20` / `1024` | Budget per discovery run or preparation package, per-request timeout, output cap. Tenants can only narrow these. |
| `AI_CACHE_ENABLED` / `AI_CACHE_DIR` | `true` / `.cache/ai` | Versioned answer cache (never expires by time; version bumps invalidate). |
| `AI_CANDIDATE_DATA_PROVIDERS` | `ollama` | Hosted providers allowed to see candidate-side text (the L2 polisher). Local providers are always allowed. Gemini must be listed explicitly before it may polish. |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Deliberately configurable: Google retires model aliases on its own schedule, and pinning one in code would break silently later. |
| `SIGNAL_EXCERPT_CHARS` / `SIGNAL_EXCERPT_RETENTION_DAYS` | `2000` / `180` | Signal Inbox (Phase 10): how much normalized, credential-redacted text of a supplied message is stored, and after how many days `POST /api/v1/signals/purge-excerpts` drops the excerpt of settled signals (hashes, classification, attribution and outcomes stay). |
| `STALE_RUN_TIMEOUT_MINUTES` | `60` | How long a discovery run can sit at `running` before it's reconciled as `interrupted` on next boot/run. |
| `CLOSURE_MAX_FAILURE_RATIO` | `0.25` | Above this failed-job fraction, the stale-job sweep refuses to close anything (observed set considered incomplete). |
| `SEMANTIC_MATCHING_ENABLED` | `false` | Off by default; requires the `semantic` extra when turned on. |

See `.env.example` for the full list (discovery concurrency, retry/backoff,
per-source rate limit, request timeout, Firecrawl settings, match policy
version, semantic matching thresholds).

---

## Running the app

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### The desktop control center (one process, loopback only)

```bash
pip install -e ".[desktop]"        # pywebview (WebView2 on Windows); optional
alembic upgrade head               # the shell never migrates for you
python -m app.desktop              # server on 127.0.0.1:<free port> + native window
python -m app.desktop --worker     # also run the local Playwright worker in DRY RUN (needs the browser extra)
python -m app.desktop --worker --headed   # dry run with a visible browser
python -m app.desktop --no-window  # server (and worker) only; stop with Ctrl-C / Ctrl-Break
python -m app.desktop --no-native-notifications   # in-app notification list only, never a Windows toast
python -m app.desktop --no-notifications          # do not poll for notifications at all
python -m app.desktop --notification-interval 30  # seconds between notification polls (default 15)
python -m app.desktop --install-shortcut          # create/refresh the "CareerOS" Desktop shortcut and exit
```

#### The `CareerOS` desktop shortcut (Windows)

Run this once from the project's environment (any current directory works):

```powershell
C:\path\to\Range-Apply\.venv\Scripts\python.exe -m app.desktop --install-shortcut
```

It writes `CareerOS.lnk` to your Desktop (the folder Windows reports, so a
OneDrive-redirected Desktop is found too) and prints exactly where it went and
what it runs:

```
Desktop shortcut: C:\Users\<you>\Desktop\CareerOS.lnk
  runs:    C:\path\to\Range-Apply\.venv\Scripts\pythonw.exe -m app.desktop --worker --headed
  in:      C:\path\to\Range-Apply
```

Double-clicking it starts the normal control center — window, local worker in
DRY RUN, visible browser — with the project's own interpreter and the
repository as working directory. `pythonw.exe` means no console window; the
log then goes to `DESKTOP_STATE_DIR/desktop.log` (default
`.cache/desktop/desktop.log`) and a startup problem (for example an
unmigrated database) is shown in a message box. The shortcut carries no key,
cookie or token: configuration still comes from `.env`. It needs no
administrator rights, runs nothing but the documented entry point, and running
the installer again overwrites the same file (one shortcut, never
duplicates). `--shortcut-dir <folder>` writes it elsewhere. After moving the
repository or recreating `.venv`, run the installer again.

**One CareerOS at a time.** The entrypoint holds an OS-level lock,
`DESKTOP_STATE_DIR/desktop.lock`, for as long as it runs; `desktop.json` beside
it records only the pid, port and start time. Launching again while CareerOS
is open starts nothing: the open window is brought to the front (or you are
told where CareerOS is running) and the second launch exits with code 3. The
operating system releases the lock when the process ends, however it ends, so
a crashed instance never blocks the next start; a process that still holds
the lock but no longer answers is named, with its pid, instead of being
mistaken for a running copy.

What it does, in order: readiness (database reachable and migrated,
`API_KEY` set unless `APP_ENV` is development), uvicorn in a thread on
`127.0.0.1` (`API_PORT` when free, otherwise a free port), `/health` polled
until it reports `ok`, the optional worker in its own thread, then the
window at a single-use `/desktop/login?token=…` URL that sets the same
HttpOnly dashboard cookie a `?key=` sign-in sets — the API key itself never
appears in a URL or a log line. Closing the window stops the worker (and its
browser), then the server. `GET /desktop/status` (dashboard cookie) reports
server, readiness and worker state as flags and counts only.

The shell never creates or writes credentials: if `API_KEY` is unset outside
development it refuses to start and tells you to set it in `.env`. Pages
served inside the shell perform writes with the dashboard cookie plus the
header `X-Requested-With: careeros-desktop` (both required; the API key is
never embedded in HTML or scripts); API clients and the extension keep using
`X-API-Key`.

The control center itself lives at `/desktop/*` (`app/desktop/views.py`,
Jinja2 + a vendored htmx + one small script, no internet needed): Home,
Opportunities (search and filters over the existing repository — never a
top-N), Applications (queue-like groups with Review / Retry / Cancel / Open
in browser / Inspect execution / Inspect audit), the Application Review page
("Why this job?" from the eligibility decision, fit, priority and admission
records with the evidence grades CONFIRMED / APPROXIMATE / UNVERIFIED /
NEEDS_REVIEW / REMOVED; "What will be submitted?" from the preparation,
rendered documents with SHA-256, prepared answers and mapped form fields —
missing required answers are shown as NEEDS USER INPUT with an answer box,
never filled in), Attention (every handoff, uncertain, failed, needs-input
and review-signal item with *what happened / why it needs you / what you
can do*), Documents (versions, hashes, validation, open inline, regenerate,
invalidate) and System (services, execution mode, diagnostics). Career
Brain, Signals, Learning and Diagnostics link to the existing dashboards.
Every button posts to the existing API route it names in `data-url`.

#### Real submission (Increment 4)

A real submission is only ever started by you, from one screen, for one
application:

1. **Review** (`/desktop/applications/{id}`) offers two clearly separate
   buttons for a READY attempt that has a preparation: `[Dry run]` and the
   red `[Submit for real]`. Neither submits anything; both open the
   confirmation screen. While a run is in progress the buttons are replaced
   by a link to that run.
2. **Confirm** (`/desktop/applications/{id}/confirm?mode=dry_run|live`)
   shows the whole package before anything happens: company, role, attempt
   number, target URL (with an *Open in browser* link), the exact artifact
   type / version / SHA-256 that would be uploaded (or "not rendered yet —
   rendering happens when execution starts"), every prepared answer with
   ANSWERED / NEEDS USER INPUT and its evidence grades, the mapped form
   fields if a form was already discovered, the pre-submit preconditions,
   and the execution mode (*HEADED PLAYWRIGHT — a visible Chromium window on
   this machine*, DRY RUN vs LIVE). In `mode=live` a red box states "This
   will submit a real job application to the employer." and the red button
   stays unusable until you type `SUBMIT` into the confirmation input
   (`data-require-input` / `data-require-value` in `static/desktop.js`; the
   form carries `onsubmit="return false"` so Enter never submits).
3. The button posts `{"mode": …, "confirm": "SUBMIT"}` to
   `POST /desktop/api/attempts/{id}/run` (cookie + desktop header, never an
   API key) and follows the `job_url` the 202 returns
   (`data-next-from="job_url"`). The run supervisor (`app/desktop/runner.py`)
   takes the attempt through the **existing** claim → precondition gate →
   `ExecutionService.execute` path with a headed Playwright executor: one
   attempt, one visible window, nothing queued behind it.
4. **Result** (`/desktop/applications/{id}/run/{job_id}`, polling
   `/desktop/partials/run/{job_id}` every 2s while it runs) says in one
   line what happened: SUBMITTED / VERIFIED, SUBMITTED / LIKELY, UNKNOWN /
   UNCERTAIN, BLOCKED, NEEDS REVIEW, DRY RUN COMPLETE, RETRYABLE or
   PERMANENT FAILURE — plus the run id, submit-pressed flag, verification
   status / method / detail, confirmation reference, application URL, the
   signal the run created and the current derived outcome.

The rules the rest of the system already enforces are visible here, not
re-implemented: an **UNKNOWN** outcome is never resubmitted automatically —
the page says so and offers the two existing confirm buttons
(`POST /api/v1/execution/runs/{id}/confirm`); a CAPTCHA, a login or a
one-time code is a **handoff** (what happened / why it needs you / what you
can do, with an *Open in browser* link), never an automated bypass; nothing
is called a success without `VERIFIED` or a LIKELY verification. A dry run
never presses the submit control. `PLAYWRIGHT_HANDOFF_WAIT_SECONDS` governs
how long the visible browser waits for you before it closes.

#### Local notifications (Increment 5)

The shell tells you when something needs you or happened without you —
without a new table, a second worker, e-mail or AI. Everything lives in
`app/desktop/notifications/`:

* **Derived, never stored.** `events.py` reads three existing sources since
  the last cursor and turns them into `Notification` values (`model.py`):
  attempt transitions in `application_events` (READY → *Application Ready*;
  BLOCKED / NEEDS_USER_INPUT / NEEDS_REVIEW / AWAITING_APPROVAL →
  *Attention Required* with the handoff reason — CAPTCHA, login, MFA,
  unsupported or ambiguous form, unknown required field, missing document;
  SUBMITTED → *Application Submitted*; VERIFIED / UNCERTAIN → *Verification
  Result*, the UNCERTAIN text says CareerOS will not resubmit; FAILED →
  *Execution Failed*; INTERVIEWING / REJECTED → the signal kinds), finished
  execution runs that failed retryably and left the attempt READY (*Execution
  Failed*, without the error text), and ingested `signals` by category
  (INTERVIEW_INVITATION, REJECTION, RECRUITER_CONTACT, ASSESSMENT,
  INFORMATION_REQUEST; execution's own EXECUTION_RESULT / PLAYWRIGHT rows are
  skipped). A READY that immediately follows SUBMITTING (a retryable failure
  handing the attempt back) is not announced twice. Text carries company,
  role, status and reason words only — never a signal's subject, sender or
  excerpt, an answer, a document, a hash, an employer URL, an error message
  or a key. Reasons must be bare vocabulary tokens; page text in metadata is
  never echoed.
* **Cursor file, not a table.** `cursor.py` keeps one small JSON file,
  `DESKTOP_STATE_DIR/notifications.json` (default `.cache/desktop/`,
  gitignored): per tenant and per source a watermark timestamp plus the row
  ids seen at that exact timestamp (timestamps can tie within a clock tick).
  It is written atomically (temp file + `os.replace`), a missing or corrupt
  file starts fresh at *now* (a first launch or a restart never floods you
  with history), and it holds nothing but watermarks and ids.
* **One poller, one thread.** `poller.py` runs every 15 s (configurable)
  under the same `WorkerSupervisor` as the optional worker, opens a short
  session per poll, never writes to the database, survives a bad poll, and
  is stopped first at shutdown. `--no-notifications` disables it.
* **In-app always, native when possible.** `center.py` is an in-memory,
  per-tenant, bounded (200) list with an unseen count: the sidebar shows a
  *Notifications* badge, `/desktop/notifications` lists them newest first
  with an *Open* link to the relevant page (review page for an application,
  the signal page for an unmatched signal), and `[Mark all as seen]` posts
  to `POST /desktop/api/notifications/seen` (cookie + desktop header; it
  touches memory only). `native.py` shows a Windows toast through
  [`winotify`](https://pypi.org/project/winotify/) when it is installed (part
  of the `desktop` extra on Windows) — `CareerOS — Application Ready` /
  *Your application for Software Engineer at Example Corp is ready for
  review.* — and falls back silently to the in-app list anywhere else. The
  adapter XML-escapes the payload and neutralises PowerShell expansion
  (winotify builds its toast through an expandable PowerShell template):
  without that, an `&` in the click URL silently dropped the toast and a
  `$env:…` in a company name would have been expanded.
* **Click = navigate, nothing more.** A toast's click URL is
  `http://127.0.0.1:<port>/desktop/open?to=/desktop/applications/<id>&n=<nonce>`:
  the nonce is single-use and bounded, `to` must be a bare local
  `/desktop/…` or `/dashboard/…` path, no cookie is ever set, nothing runs.
  With the window open the route points the already-signed-in window at the
  page (pywebview's `load_url` marshals onto the GUI thread) and the browser
  tab that Windows opened just says so; with `--no-window` it redirects and
  the page's own dashboard authentication applies.

Notifications are informational: nothing in the package can start, retry,
cancel or change an application, a policy, a cap or an outcome. Tests:
`tests/desktop/test_notification_events.py`, `test_native_notifications.py`,
`test_notification_center.py`, `test_notification_poller.py`,
`test_notification_privacy.py`; the child-process test proves the poller runs
and stops with the entrypoint.

On startup the lifespan handler (`app/main.py`) does three things, in order:
verifies the schema (`verify_schema()` — fails fast per above), reconciles
any discovery run left stuck at `running` by a previous crash
(`reconcile_stale_runs`), and logs a loud warning if `API_KEY` is unset
outside development.

### Reaching the dashboard

The dashboard (`/dashboard/*`, plus the profile editor under
`/dashboard/profile/*`) requires the same `API_KEY` as write endpoints, but a
browser navigation can't set a custom header. Sign in by visiting:

```
http://localhost:8000/dashboard/?key=YOUR_API_KEY
```

The first request with `?key=...` sets an HttpOnly `careeros_key` cookie
(30-day expiry); subsequent navigation doesn't need the query parameter.
Without a valid key you get a small sign-in page (HTML 401), not a bare JSON
error.

### Triggering discovery

```bash
# Background (default) — returns 202 immediately, poll for progress
curl -X POST http://localhost:8000/api/v2/discovery/run \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"source": "GREENHOUSE", "identifier": "stripe"}'

curl http://localhost:8000/api/v2/discovery/runs

# Synchronous — holds the connection open until the run finishes.
# Only reasonable for a small board; a large one can take minutes.
curl -X POST "http://localhost:8000/api/v2/discovery/run?wait=true" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"source": "GREENHOUSE", "identifier": "stripe"}'

# Several boards in one call (what the scheduled GitHub Actions workflow uses)
curl -X POST http://localhost:8000/api/v2/discovery/run-batch \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"targets": [{"source": "GREENHOUSE", "identifier": "stripe"}, {"source": "LEVER", "identifier": "ramp"}]}'
```

### Importing the Career Brain seed

The first `CareerBrainService` load bootstraps the default tenant from
`data/career_seed.json` automatically. To re-import after editing the seed
(idempotent: unchanged records are skipped, changed ones are updated with an
audit event):

```bash
python -m app.career.importer                 # default tenant, CAREER_DATA_PATH
python -m app.career.importer --force          # re-apply every record
curl -X POST http://localhost:8000/api/v1/career/import -H "X-API-Key: $API_KEY" -d '{}'
```

Evidence, positioning variants and the answer bank are edited at
`/dashboard/profile/` or under `/api/v1/career/*`.

### Polling, capture, resume, source health

```bash
# Register a board for polling (run-batch does this too), then poll only what is due
curl -X POST http://localhost:8000/api/v2/discovery/sources -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"source": "GREENHOUSE", "identifier": "stripe", "company_name": "Stripe", "poll_interval_minutes": 240}'
curl -X POST http://localhost:8000/api/v2/discovery/run-due -H "X-API-Key: $API_KEY"

# Ingest a captured page (what the extension will send)
curl -X POST http://localhost:8000/api/v2/discovery/capture -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://jobs.example.com/acme/123", "title": "Backend Engineer", "company": "Acme", "location": "Remote", "description": "..."}'

# Resume an interrupted/partial run without re-processing what it already saw
curl -X POST http://localhost:8000/api/v2/discovery/runs/<run_id>/resume -H "X-API-Key: $API_KEY"

curl http://localhost:8000/api/v2/discovery/sources      # health, back-off, next poll
```

Source health and back-off are visible at `/dashboard/sources`; per-run
volume and AI counters at `/dashboard/runs`.

### Preparing application packages

```bash
# Prepare one admitted candidate opportunity (level from the policy's tailoring_by_band)
curl -X POST http://localhost:8000/api/v1/preparations/ -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" -d '{"candidate_opportunity_id": "<co_id>"}'
# Batch, forcing L0 for a volume lane
curl -X POST http://localhost:8000/api/v1/preparations/batch -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" -d '{"candidate_opportunity_ids": ["..."], "tailoring_level": "L0"}'
# Drain PREPARE queue items
curl -X POST http://localhost:8000/api/v1/preparations/run-queue -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" -d '{"worker_id": "local", "limit": 20}'
curl http://localhost:8000/api/v1/preparations/<id>/evidence   # why each block is there
```

Packages, answers needing your input, and approve/reject/regenerate live at
`/dashboard/preparations`. Set cover-letter modes per band via
`PUT /api/v1/policy/` (`cover_letter_by_band`: DISABLED | TEMPLATE | LIGHT | TARGETED).

```bash
pytest -q tests/preparation/test_perf.py -s                       # 1,000 opportunities, L0 + L1, zero AI
CAREEROS_PERF=1 pytest -q tests/preparation/test_perf.py -s       # 5,000 (opt-in)
```

### Scheduling (admission under the policy)

```bash
curl "http://localhost:8000/api/v1/scheduler/preview?window=500&limit=50"   # dry run: decisions in order, no writes
curl http://localhost:8000/api/v1/scheduler/status                          # caps used/remaining today + this week, cool-downs, last run
# One scheduling pass: reconcile, admit in priority order, enqueue PREPARE (and optionally prepare, zero-AI)
curl -X POST http://localhost:8000/api/v1/scheduler/run -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" -d '{"window": 500, "prepare": true, "prepare_limit": 100}'
curl http://localhost:8000/api/v1/scheduler/runs                            # history; /runs/<id> for one record
curl http://localhost:8000/api/v1/scheduler/queue                           # queue by state/action + execution-ready count
```

Semantics worth knowing: a cap counts **admitted attempts** in the tenant's
`timezone` day / ISO week (`PUT /api/v1/policy/` with `timezone`,
`minimum_fit_score`); `0` pauses; released attempts free their slot. The
`window` is a batch size, not a cap — deferred work is reported as
`WINDOW_DEFERRED` and picked up by the next run. `/dashboard/scheduler` shows
the same preview, capacity, cool-downs and run history.

```bash
pytest -q tests/scheduler/test_perf.py -s                         # 500 mixed candidate opportunities, always
CAREEROS_PERF=1 pytest -q tests/scheduler/test_perf.py -s         # 5,000 (opt-in)
```

### Executing READY attempts (no browser automation yet)

```bash
curl http://localhost:8000/api/v1/execution/ready                                  # READY attempts
curl http://localhost:8000/api/v1/execution/attempts/<application_id>/preview      # package + preconditions + form mapping, no writes
curl -X POST http://localhost:8000/api/v1/execution/enqueue-ready -H "X-API-Key: $API_KEY"   # (the scheduler also does this)
# External executor flow (what the extension / local runner will speak):
curl -X POST http://localhost:8000/api/v1/execution/claim -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{"worker_id": "ext", "limit": 1}'
curl -X POST http://localhost:8000/api/v1/execution/items/<item_id>/start -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{"worker_id": "ext", "executor": "MANUAL"}'
curl -X POST http://localhost:8000/api/v1/execution/items/<item_id>/form ...      # discovered fields -> mapped answers
curl -X POST http://localhost:8000/api/v1/execution/items/<item_id>/result ...    # {"worker_id": "ext", "result": {"outcome": "SUBMITTED", "submit_attempted": true, "confirmation_reference": "..."}}
curl -X POST http://localhost:8000/api/v1/execution/items/<item_id>/handoff ...   # CAPTCHA_REQUIRED / AUTH_REQUIRED / MFA_REQUIRED / ...
curl -X POST http://localhost:8000/api/v1/execution/runs/<run_id>/verify -H "X-API-Key: $API_KEY"
curl -X POST http://localhost:8000/api/v1/execution/runs/<run_id>/confirm -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{"submitted": true, "reference": "..."}'
# In-process executor (MOCK needs EXECUTION_MOCK_ENABLED=true or APP_ENV=test; MANUAL always hands off):
curl -X POST http://localhost:8000/api/v1/execution/run-queue -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{"worker_id": "local", "limit": 10, "executor": "MANUAL"}'
```

`/dashboard/execution` shows attempts by status, handoffs, runs, and lets you
answer a blocked form field, confirm an outcome, retry or cancel.

```bash
pytest -q tests/execution/test_perf.py -s                         # 1,000 READY attempts through the mock executor, always
CAREEROS_PERF=1 pytest -q tests/execution/test_perf.py -s         # 5,000 (opt-in)
```

### Rendering documents (Blueprint Phase 8)

Every READY preparation can become a real PDF (and DOCX) resume / cover
letter. Rendering is deterministic layout over the validated blocks: no AI,
no rewording, and a render that loses any line of content is refused.

```bash
curl -X POST http://localhost:8000/api/v1/documents/render -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" -d '{"preparation_id": "<prep_id>", "artifact_type": "RESUME", "format": "PDF"}'
curl -X POST http://localhost:8000/api/v1/documents/ensure/<prep_id> -H "X-API-Key: $API_KEY"   # resume + cover letter (when enabled)
curl http://localhost:8000/api/v1/documents/by-preparation/<prep_id>                            # versions, hashes, validation
curl -o resume.pdf http://localhost:8000/api/v1/documents/<artifact_id>/file -H "X-API-Key: $API_KEY"
curl -X POST http://localhost:8000/api/v1/documents/<artifact_id>/materialize -H "X-API-Key: $API_KEY"  # verify hash/size/type
curl -X POST http://localhost:8000/api/v1/documents/<artifact_id>/invalidate -H "X-API-Key: $API_KEY" -d '{"reason": "..."}'
```

Files live under `DOCUMENTS_ROOT` (default `data/documents/`, git-ignored) as
`<tenant>/<candidate-opportunity>/<preparation>/resume-v1.pdf`; they are
written once and never modified. The execution worker renders or reuses
them automatically before an upload and re-checks the SHA-256 right before
the browser receives the file. Settings: `DOCUMENTS_ROOT`,
`DOCUMENTS_FONT_PATH` (TrueType font, embedded only when the content needs
non-Latin-1 glyphs), `DOCUMENTS_MAX_PAGES_RESUME` / `_COVER_LETTER`,
`DOCUMENTS_MAX_BYTES`, `DOCUMENTS_LETTER_DATE` (optional fixed date line).
The preparation page on the dashboard lists documents and can render them.

```bash
pytest -q tests/documents                                  # rendering, storage, hashing, determinism, API
pytest -q tests/documents/test_perf.py -s                  # 100 resumes + 100 cover letters, always
CAREEROS_PERF=1 pytest -q tests/documents/test_perf.py -s  # 500 (opt-in)
```

### Local browser execution (Playwright, Blueprint Phase 7)

```bash
pip install -e ".[browser]"
python -m playwright install chromium                 # local browser, ~150 MB, once

# Dry run (the default): navigate, discover the form, map answers, report; never press submit.
python -m app.execution.worker --tenant default --once --dry-run
# Live, one item, visible browser so you can clear a CAPTCHA/login yourself if asked:
python -m app.execution.worker --tenant default --once --live --headed
# Keep polling the queue (Ctrl-C stops cleanly and closes the browser):
python -m app.execution.worker --tenant default --live
```

What it does per attempt: claims a SUBMIT item, builds the `ExecutionPackage`,
opens the application URL, discovers the form into `form_snapshots` /
`form_fields`, fills only fields with a safe prepared answer, uploads only a
real local document (`EXECUTION_RESUME_FILE`, `EXECUTION_COVER_LETTER_FILE`),
re-runs every precondition, clicks submit (unless dry-run), then classifies
the result conservatively (confirmation → SUBMITTED/VERIFIED, form error →
NEEDS_REVIEW, nothing observed → UNCERTAIN, challenge → BLOCKED handoff). It
never solves a CAPTCHA, automates MFA or stores credentials. Supported
targets: Greenhouse, Lever, generic forms; Ashby only when the page uses
standard controls (otherwise `UNSUPPORTED_FORM` handoff).

Real boards (Phase 13): the form may live in an embedded ATS iframe
(Greenhouse on company career sites) or render client-side after the load
event (Ashby); discovery scans child frames and keeps re-scanning for up to
`PLAYWRIGHT_SETTLE_MS` (8000). A page whose only form is a search box or a
cookie banner (a closed posting redirected to the board index) is an
`AMBIGUOUS_FORM` handoff, never filled. A *visible* CAPTCHA widget inside
the form hands off before anything is typed; once a person has completed it
in the browser window (the provider's response token is present — detected,
never produced) the run continues. An *invisible* widget (Greenhouse-hosted
boards' reCAPTCHA badge, Lever's invisible-mode hCaptcha) is reported in
the diagnostics but is not a wall: the provider scores the submit itself,
and a challenge it shows after the click hands off after the click.

Settings (`.env`): `PLAYWRIGHT_DRY_RUN`, `PLAYWRIGHT_HEADLESS`,
`PLAYWRIGHT_*_TIMEOUT_MS`, `PLAYWRIGHT_SUBMIT_WAIT_MS`, `PLAYWRIGHT_SETTLE_MS`,
`PLAYWRIGHT_MIN_DELAY_SECONDS`, `PLAYWRIGHT_PROFILE_DIR` (a *local* persistent
profile for signed-in sessions; keep it out of git and backups),
`PLAYWRIGHT_HANDOFF_WAIT_SECONDS` (headed mode: how long to wait for you to
clear a challenge), `PLAYWRIGHT_DEBUG_ARTIFACTS_DIR` (opt-in handoff
screenshots, local only, may contain your data).

```bash
pytest -q tests/execution/test_playwright_discovery.py tests/execution/test_playwright_executor.py   # local HTML fixtures, no network
pytest -q tests/execution/test_playwright_perf.py -s                 # 100 local forms, always
CAREEROS_PERF=1 pytest -q tests/execution/test_playwright_perf.py -s # 500 (opt-in)
```

Tests skip cleanly when Playwright is not installed. No live employer site is
part of the suite; there is no opt-in live smoke test because no public test
form that is safe to submit against exists.

### Browser extension (Blueprint Phase 9)

The second executor runs in your own browser: `extension/` is a Manifest V3
extension in plain JavaScript (no framework, no bundler, no `npm`). Load it
unpacked (`chrome://extensions` → Developer mode → Load unpacked → the
`extension/` folder), open its **Options**, enter `http://127.0.0.1:8000` and
your `API_KEY`, press *Test connection*. Then run the scheduler so READY
attempts become SUBMIT items, open an application page from the execution
dashboard, click the extension (badge **1**) → **Fill this application**.

Submit modes (options): `manual` (default — the extension fills, you press
the site's submit button; the click is intercepted and released only after
the server gate passes), `auto` (the extension presses submit once every
mapped field is filled and the gate passes), `dry_run` (fill only; the
submit is blocked and a `DRY_RUN` run is recorded). Fields with no truthful
prepared answer are listed in the popup: answer them there (optionally saved
to your answer bank) or on the page. CAPTCHA / login / MFA / missing document
→ handoff: finish yourself, then *I submitted it* / *I did not submit*.

The extension talks only to the local API (`X-API-Key`, CORS grants
`chrome-extension://` and `moz-extension://` origins only), never to the
database; the key lives in the extension's storage, not in the source. See
`extension/README.md`.

```bash
pytest -q tests/execution/test_extension_api.py     # the HTTP flow as the service worker performs it
pytest -q tests/execution/test_extension_page.py    # page scripts under Playwright against the local fixtures
pytest -q tests/execution/test_extension_perf.py -s # 50 local forms through the extension flow (200 with CAREEROS_PERF=1)
```

`extension/src/discover.js` is the single form-discovery script: the
Playwright executor evaluates the same file, so a discovery fix lands in both
executors at once.

### AI Gateway, Tier-1 gates and fit bands (Blueprint Phase 8b)

AI is optional everywhere. With the defaults (`AI_ENABLED=false`) discovery,
matching, admission, preparation, execution and verification make **zero**
AI calls; the gateway answers `DISABLED` in microseconds and every caller
takes its deterministic path (L2 becomes L1-equivalent output, extraction
keeps `UNKNOWN` fields as `UNKNOWN`).

To try AI: set `AI_ENABLED=true` and `AI_PROVIDER=gemini` (with
`GEMINI_API_KEY`) or `AI_PROVIDER=ollama` (a local model server), restart,
then turn it on per candidate on the policy page (or
`PUT /api/v1/ai/config {"enabled": true}`). Candidate-side text (the L2
polisher) only goes to local providers or those in
`AI_CANDIDATE_DATA_PROVIDERS`; job-side extraction follows the global switch.

```bash
curl http://localhost:8000/api/v1/ai/config                 # global (no secrets) + tenant settings + what is effective
curl http://localhost:8000/api/v1/ai/status                 # gateway counters, tenant usage by status/operation, recent calls
curl http://localhost:8000/api/v1/ai/usage?operation=polish_text
curl -X POST http://localhost:8000/api/v1/ai/cache/clear -H "X-API-Key: $API_KEY"
curl http://localhost:8000/api/v1/policy/fit-bands          # thresholds, ranges, enabled bands, version
curl -X PUT http://localhost:8000/api/v1/policy/fit-bands -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{"thresholds": {"HIGH": 75, "MEDIUM": 40}}'
curl http://localhost:8000/api/v1/policy/gates              # the Tier-1 ruleset with current parameter values
```

Every AI call is accounted in `ai_usage` (operation, provider, model,
status, cache hit, latency, tokens; never the prompt or the output) and, for
preparations, summarised in the preparation's audit event (`ai`). AI output
never becomes evidence: an L2 rewrite that adds any unsupported claim is
rejected by the same validator that gates every block, and the deterministic
sentence is kept.

Fit bands are the policy's `band_thresholds` / `enabled_bands` /
`minimum_fit_score`, versioned with the policy; each candidate opportunity
records `fit_policy_version`, `admission_policy_version` and
`gate_ruleset_version`, so a threshold change re-bands only at the next
sync and never rewrites a stored match or an earlier decision.

```bash
pytest -q tests/ai tests/pipeline/test_gates.py tests/pipeline/test_fit_bands.py
pytest -q tests/ai/test_perf.py -s                       # AI-disabled path, cache-hit path, 1,000-opportunity gates
CAREEROS_PERF=1 pytest -q tests/ai/test_perf.py -s       # 5,000 opportunities
```

### Signal Inbox: verification, outcomes and review (Blueprint Phase 10)

Execution results (Phase 6/7/9 verification) become signals automatically.
Employer emails, status-page observations and your own notes are *supplied*;
nothing reads a mailbox. Everything is deterministic and works with AI off;
with the tenant's AI on, only signals the rules could not classify
confidently are sent to the gateway (`CLASSIFY_SIGNAL`, category only, never
authoritative).

```bash
# a pasted employer email (message id = stable identity; a repeat is an observation, not a new signal)
curl -X POST http://localhost:8000/api/v1/signals/email -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"message_id": "<abc@acme.com>", "sender": "talent@acme.com", "subject": "Interview invitation", "text": "We would like to invite you to an interview for the Backend Engineer role at Acme. Application ID: ACME-123", "received_at": "2026-09-17T09:00:00Z"}'
# your own note about an application you know
curl -X POST http://localhost:8000/api/v1/signals -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"source": "MANUAL", "text": "Recruiter called: rejected", "category": "REJECTION", "hints": {"application_id": "<attempt id>"}}'
curl "http://localhost:8000/api/v1/signals?review=true"          # unmatched / ambiguous / uncertain signals
curl http://localhost:8000/api/v1/signals/<id>/trace              # signal -> attribution -> opportunity -> ... -> application history
curl http://localhost:8000/api/v1/signals/outcomes                # derived current status per application
curl http://localhost:8000/api/v1/signals/outcomes/<application_id>   # the append-only event history
curl -X POST http://localhost:8000/api/v1/signals/<id>/link -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{"application_id": "<attempt id>"}'
curl -X POST http://localhost:8000/api/v1/signals/<id>/confirm-outcome -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{"outcome": "INTERVIEW_SCHEDULED"}'
```

The dashboard has the same at `/dashboard/signals` (filters: needs review,
status, source, category, outcome, company, application, last N days) and
`/dashboard/signals/{id}` (full trace, actions, event and attribution
history). Rules of thumb: a signal is linked only when exactly one
application fits; a rejection needs STRONG evidence (a confident rule, an
execution verification or your confirmation) to become the status; a weak
signal is shown as provisional and never downgrades; conflicting evidence
is NEEDS_REVIEW with the conflict listed. Nothing here changes caps,
bands, thresholds or volume.

```bash
pytest -q tests/signals                                   # ingestion, classification, attribution, outcomes, execution, review, truth boundary
pytest -q tests/signals/test_perf.py -s -m perf           # 1,000 signals / duplicate-heavy / component benchmarks, AI disabled
CAREEROS_PERF=1 pytest -q tests/signals/test_perf.py -s -m perf   # 5,000 signals
```

### Outcome learning (Blueprint Phase 11)

The learning engine reads the Phase 10 outcome history and produces
observed rates per source / company / title / role family / fit band /
lane / tailoring level / cover-letter mode / positioning variant /
execution method, response-time medians, versioned snapshots and phrased
recommendations. Everything carries a sample size, an evidence mix, a
confidence label and a window; nothing it produces changes what is applied
to. It makes no AI calls.

```bash
curl http://localhost:8000/api/v1/learning/compute                 # what the engine says now (writes nothing)
curl -X POST http://localhost:8000/api/v1/learning/snapshots -H "X-API-Key: $API_KEY"   # persist a versioned snapshot
curl http://localhost:8000/api/v1/learning/snapshots/latest        # snapshot + every metric + recommendations
curl "http://localhost:8000/api/v1/learning/snapshots/<id>?dimension=COMPANY&metric=interview_rate"
curl http://localhost:8000/api/v1/learning/recommendations
curl "http://localhost:8000/api/v1/learning/dataset?limit=20"       # the rows, with evidence quality per row
curl http://localhost:8000/api/v1/learning/expected/<candidate opportunity id>   # the ordering signal and its components
# opt in to learned ordering (order only; default off). Audited as a policy update.
curl -X PUT http://localhost:8000/api/v1/learning/settings -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{"ordering_enabled": true, "window_days": 180}'
```

Statistics: a group's rate is shrunk toward the candidate's own overall
rate by `prior_strength` pseudo-observations (10 by default) and shown with
a 95 % Wilson interval; fewer than `min_samples` (5) applications is LOW
confidence and never a recommendation. Labels use only outcome events
whose Phase 10 evidence is at least `minimum_evidence` (MODERATE; set WEAK
for exploratory views). A snapshot at `as_of` only sees events observed by
then, so historical snapshots never contain later knowledge.

With `ordering_enabled`, the next opportunity sync feeds the learned
`expected_response` (response and interview rates against the baseline,
confidence-weighted, 50 = neutral) into the existing `learned_prior`
priority component (weight 0.05 unless you change `priority_weights`).
Admission, caps, bands, thresholds, blocklists and eligibility are never
touched, and there is no top-N. `/dashboard/learning` shows the same with
sample sizes everywhere.

```bash
pytest -q tests/learning                                   # statistics, dataset, leakage, learning, safety, API
pytest -q tests/learning/test_perf.py -s -m perf           # 1,000 / 5,000 applications
CAREEROS_PERF=1 pytest -q tests/learning/test_perf.py -s -m perf   # 10,000
```

### Operations, hardening and benchmarks (Blueprint Phase 12)

`docs/OPERATIONS.md` is the operator's page: invariants, the execution
safety rules, startup recovery, health, backup/restore, retention, the
index review and the SQLite/PostgreSQL parity table.

```bash
curl http://localhost:8000/api/v1/ops/diagnostics          # queue depth, stale leases, uncertain attempts, caps, warnings (or /dashboard/ops)
pytest -q tests/hardening                                  # concurrency, execution safety, caps, recovery, security, failure injection, migrations, backup
pytest -q tests/hardening/test_perf_pipeline.py -s -m perf # end-to-end soak (3 rounds x 30 opportunities; CAREEROS_SOAK_ROUNDS / _SIZE)
CAREEROS_PERF=1 pytest -q tests/discovery/test_perf_fixture.py -s -m perf -k 10000   # 10,000-job discovery
```

The consolidated benchmark battery is the per-phase perf modules run in one
go: discovery (`tests/discovery/test_perf_fixture.py`), scheduler
(`tests/scheduler/test_perf.py`), preparation (`tests/preparation/test_perf.py`),
execution (`tests/execution/test_perf.py`), signals (`tests/signals/test_perf.py`),
learning (`tests/learning/test_perf.py`) and the soak above; `CAREEROS_PERF=1`
unlocks the 5,000 / 10,000 sizes. Every run reports `ai_calls: 0`.

### Volume fixture

```bash
pytest -q tests/discovery/test_perf_fixture.py -s            # 500 synthetic jobs, always
CAREEROS_PERF=1 pytest -q tests/discovery/test_perf_fixture.py -s   # 5,000 jobs (opt-in)
```

### Running a match recalculation

Discovery does not score jobs by itself — matching is a separate step against
whatever is already in the `jobs` table:

```bash
# Background (default)
curl -X POST http://localhost:8000/api/v3/matches/recalculate \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"only_stale": true}'

curl http://localhost:8000/api/v3/matches/runs

# Synchronous, and score everything (not just stale jobs)
curl -X POST "http://localhost:8000/api/v3/matches/recalculate?wait=true" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{}'
```

`only_stale: true` restricts scoring to jobs whose `content_hash` changed
since they were last scored (`stale_job_ids`), which is the cheap default for
a free-tier deployment. Results show up at `/api/v3/matches/` and in
`/dashboard/shortlist`.

Every finished match run is projected into candidate opportunities
(eligibility decision, fit band, priority, policy admission). Inspect them at
`/dashboard/opportunities` or `/api/v1/opportunities/`; set bands, caps and
lanes at `/dashboard/policy` or `PUT /api/v1/policy/`; enqueue an admitted
opportunity with `POST /api/v1/opportunities/{id}/enqueue` and watch it at
`/dashboard/queue`. To re-project an old run by hand:

```bash
curl -X POST http://localhost:8000/api/v1/opportunities/sync \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{}'
```

---

## Development Commands

```bash
# Run tests (project venv only — see repo-level agent instructions)
pytest

# Run tests with verbose output
pytest -v

# Lint (must be clean; CI enforces it)
ruff check app tests

# Format
ruff format app tests
```

### Running against PostgreSQL locally

SQLite is the zero-install default. To develop or test against the engine the
hosted deployment uses:

```powershell
docker compose up -d                       # Postgres 16 + pgvector on :5432
pip install -e ".[dev,postgres]"
$env:DATABASE_URL = "postgresql://careeros:careeros@localhost:5432/careeros"
alembic upgrade head
uvicorn app.main:app --reload
pytest -q                                  # conftest honours DATABASE_URL
```

(`export DATABASE_URL=...` on Linux/macOS.) The test suite drops nothing on a
shared Postgres database; use a dedicated database name for tests if you want
a clean slate each run.

### Errors and logging conventions

- Raise `app.core.errors.CareerOSError` subclasses (`NotFoundError`,
  `ValidationFailed`, `ConflictError`, `PolicyBlocked`, `BudgetExhausted`,
  `ExternalServiceError`, `BotCheckDetected`, ...) from services. The handler in
  `app/main.py` maps them to HTTP and a stable `error.code`; queue consumers
  read `retryable`. Do not translate them into `HTTPException` by hand.
- Never swallow an exception silently. Catch, log with `logger.exception`,
  and either re-raise or convert to a structured error.
- Never log secrets or document text. `app/core/logging.py` redacts known
  secret shapes as defence in depth; it is not a licence to log them.

---

## Project Structure

```text
Range-Apply/
├── RIBHU_CAREER_CONTEXT.md    # Canonical career context
├── data/
│   └── career_seed.json       # Structured career data (Career Brain's source of truth)
├── app/
│   ├── main.py                # FastAPI entry point, lifespan, router wiring
│   ├── config.py               # Settings
│   ├── database.py             # Engine/session, schema verification
│   ├── security.py             # API_KEY / dashboard auth
│   ├── core/                   # timeutils, errors, logging (redaction), ids
│   ├── models/                 # Phase 1 Pydantic domain models
│   ├── services/                # CareerBrainService, TruthValidator
│   ├── api/routes/              # Phase 1 REST routes
│   ├── career/dashboard/        # Profile-editing dashboard
│   ├── jobs/                    # Phase 2: discovery, normalization, dedup, ingestion
│   ├── intelligence/            # Phase 3: requirement extraction, eligibility, matching
│   ├── tailoring/               # Phase 4 (lite): deterministic artifacts, /api/v4
│   └── application/             # Phase 5/6 (lite): state machine, kill switch, /api/v5
├── alembic/                    # Migrations (authoritative schema)
├── tests/                      # Test suite
├── docker-compose.yml          # Local Postgres 16 + pgvector
├── docs/
│   ├── BLUEPRINT.md               # Revised blueprint — source of truth for implementation
│   ├── IMPLEMENTATION_CONTEXT.md  # Module map, data flow, invariants — read this first
│   ├── ARCHITECTURE.md
│   ├── DEVELOPMENT.md
│   ├── PROJECT_STATE.md
│   ├── career/                 # Career context pack
│   └── projects/                # Project documentation
├── .github/workflows/           # ci.yml, discovery.yml (free-tier cron), boards.json
├── .env.example
├── pyproject.toml
└── README.md
```

---

## Context File Workflow

1. **Implementation map:** Read `docs/IMPLEMENTATION_CONTEXT.md` first for module ownership, data flow, and invariants.
2. **High-level context:** `RIBHU_CAREER_CONTEXT.md`
3. **Operational state:** `docs/PROJECT_STATE.md`
4. **Task-specific context:** relevant files in `docs/career/` or `docs/projects/`
5. **Programmatic access:** `CareerBrainService` or the API endpoints
6. **Structured data updates:** edit `data/career_seed.json` and corresponding markdown docs

### Updating Career Data

1. Update the relevant markdown file in `docs/career/` or `docs/projects/`
2. Update `data/career_seed.json` with corresponding structured data
3. Set appropriate `verification_status` for all facts
4. Run tests to verify consistency
5. Update `docs/PROJECT_STATE.md`

---

## Updating PROJECT_STATE.md

After every meaningful change:

1. Update **Completed** or **In Progress** sections
2. Add any new **Known Issues**
3. Document **Architecture Decisions** if applicable
4. Update **Last Updated** timestamp

---

## API Documentation

When the server is running, visit:

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

---

## Testing Guidelines

- All tests must pass before marking work complete
- Test categories: Phase 1 (career brain), Phase 2 (jobs/discovery/dedup/normalization), Phase 3 (intelligence/matching)
- `tests/conftest.py` points `DATABASE_URL` at a throwaway per-process SQLite file and runs `alembic upgrade head` at import time — every test run exercises the real migrations, not a hand-rolled schema
- Do not add tests that trivially assert the obvious
- Test real behavior: identity resolution, eligibility gating (including `UNCERTAIN`), scoring reproducibility, API responses
