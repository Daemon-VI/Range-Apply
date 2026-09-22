# CareerOS Project State

## Current Phase

**Blueprint Phase 13 — real-world operational validation: complete
(uncommitted, together with Phases 0–12 and 8b).** The pipeline was run
against real public Greenhouse / Lever / Ashby boards and application pages
in dry run (never submitting); what the real world broke was fixed with
reusable changes and the compatibility matrix lives in
`docs/validation/phase13/REPORT.md`. Next: real-world operation (headed
Playwright or the extension, a person completing the handoffs).

The implementation now follows `docs/BLUEPRINT.md` (the revised CareerOS
blueprint, phases 0–13). The original PRD phases P1–P6 map onto it as
described in the blueprint §13; nothing from the PRD is removed.

## Status summary

| Area | State |
|---|---|
| PRD P1 Career Brain | Complete and now **database-backed** (Blueprint Phase 1): tenant-scoped evidence graph with grades, provenance, relationships, positioning variants, answer bank, audit ledger; `career_seed.json` is imported idempotently; `CareerBrainService` contract unchanged. |
| PRD P1.5 Foundation | Complete. Alembic-owned schema, SQLite/Postgres, Jinja2 dashboard, API key auth. |
| PRD P2 Discovery | Complete for Greenhouse/Lever/Ashby + Firecrawl fallback, 5-level exact dedup, versioning, stale-run recovery, sweep. Opportunity-level identity is Blueprint Phase 3. |
| PRD P3 Intelligence | Complete: deterministic requirement extraction, eligibility (3 gates), evidence resolution, policy-weighted fit, explanations, `/api/v3`, shortlist dashboard. Full Tier 1 gate set and fit bands are Blueprint Phase 4. |
| PRD P4 Tailoring | **Lite.** Deterministic `TailoringEngine` (`app/tailoring`), `/api/v4`, artifacts with evidence refs and approval flag. Tailoring levels, answer bank and the structural validator are Blueprint Phase 6. |
| PRD P5 Application engine | **Lite.** `ApplicationEngine` state machine, per-job idempotency, approval gate, audit events, `/api/v5`, `GreenhousePlaywrightAdapter` (dry-run only; live path unverified and deliberately stops before submit). Queue, lanes, executors, verification are Blueprint Phases 5, 8, 9, 10. |
| PRD P6 Scale | **Lite.** Global and per-source kill switch. Daily/weekly caps, cool-down, blocklist and the DB queue are enforced by the Phase 5 scheduler; executors and lanes are Phases 8/9. |
| Blueprint Phase 0 | Done (see below). |
| Blueprint Phase 1 | Done (see below). |
| Blueprint Phase 2 | Done (see below). |
| Blueprint Phase 3 | Done (see below). |
| Blueprint Phase 4 | Done (see below). |
| Blueprint Phase 5 | Done (see below). |
| Blueprint Phase 6 | Done (see below). |
| Blueprint Phase 7 | Done (see below). |
| Blueprint Phase 8 | Done (see below). |
| Blueprint Phase 9 | Done (see below). |
| Blueprint Phase 8b | Done (see below). |
| Blueprint Phase 10 | Done (see below). |
| Blueprint Phase 11 | Done (see below). |
| Blueprint Phase 12 | Done (see below). |
| Blueprint Phase 13 | Done — real-world validation (see below). |
| Desktop control center | Increments 1–5 done (shell, lifecycle, cookie-authenticated writes, control-center pages, user-controlled real submission, local notifications); controlled pilot validation + Windows shortcut done 2026-09-13; increment 6 pending. |

## Guided workflow, autopilot, Gemini and button fixes (2026-09-14)

- **Button audit** (every link, form, `data-action` and htmx call exercised
  against a seeded test database): two filter pages were broken out of the
  box — Signals "Apply" and Shortlist "Apply" answered 422 because an empty
  number box was sent — and nine more number fields (Shortlist, Opportunities,
  Scheduler, Policy) failed when cleared. All now parse through
  `app/core/params.py:to_int` (empty or invalid → default; Policy keeps the
  saved value). Test: `tests/hardening/test_blank_number_fields.py`.
- **Autopilot** (`app/desktop/autopilot.py`, `--autopilot`, on in the desktop
  shortcut): every 30 minutes (`--autopilot-interval`) discover due boards →
  score only changed jobs and sync opportunities → one scheduler pass with
  preparation (which enqueues READY attempts for the existing dry-run
  worker). A failing step is recorded by error type only; the loop never
  dies. Pause / resume / run now: `POST /desktop/api/autopilot`. It never
  calls the executor, never touches SAFE / LIVE and never submits. Tests:
  `tests/desktop/test_autopilot.py`, `test_autopilot_steps.py`.
- **Guided Home**: "Your path to applying" — profile and standard answers
  (lists exactly what is missing), find jobs (autopilot state and controls),
  match and prepare (freshness, Re-run matching), answer open questions,
  review and dry run, submit (SAFE / LIVE explained). Navigation is two
  groups: *Job search* and *More* (technical dashboards).
- **Gemini**: `.env` (git-ignored) sets `AI_ENABLED`, `AI_PROVIDER=gemini`,
  `GEMINI_MODEL=gemini-3.1-flash-lite` (2.5/2.0 flash are closed to new keys)
  and, with the candidate's consent, `AI_CANDIDATE_DATA_PROVIDERS=ollama,gemini`;
  the default tenant's policy `ai_settings.enabled=true, provider=gemini`
  (policy v2). `tests/conftest.py` forces AI off so tests never call a provider.

## Safety fix before the first real submission — SAFE / LIVE switch (2026-09-14)

**Before:** the banner's "SAFE / DRY RUN" came from `PLAYWRIGHT_DRY_RUN`
(the worker/executor default), but the desktop run route accepted
`mode=live` + `confirm=SUBMIT` whatever the banner said — SAFE was a label.

**After:** a server-side, per-tenant, in-memory switch
(`app/execution/submission_mode.py`). Every process starts **SAFE**; nothing
on disk, in the environment or in a request makes LIVE the default. Only
`POST /desktop/api/submission-mode {"mode": "live"}` (desktop cookie +
`X-Requested-With` header, or `X-API-Key`) turns on **LIVE SUBMISSION
ENABLED**; `{"mode": "safe"}` turns it off immediately. Enforced at three
places, none trusting the UI:

- `POST /desktop/api/attempts/{id}/run` → **403** for `mode=live` while SAFE
  (checked before the typed-SUBMIT check, which still applies in LIVE → 400);
- `LocalRunner.start` → `LiveSubmissionDisabled` for a live job while SAFE;
- `ExecutionService._pre_submit_gate` → failure `SAFE_MODE` while SAFE. That
  gate runs immediately before every click (Playwright executor, extension
  `/extension/gate`, mock executor), so switching back to SAFE mid-run stops
  the click; a dry run returns before the gate and is unaffected.

The standalone worker needs `--live` (its process' explicit switch);
`PLAYWRIGHT_DRY_RUN=false` without `--live` refuses to start. The legacy v5
route already refuses live. The banner on every desktop page shows the
switch (Enable live submission / Return to SAFE); the confirmation screen
shows no SUBMIT form while SAFE; the extension status carries
`live_submission_enabled`. No gate was weakened. Tests:
`tests/desktop/test_submission_mode_safety.py` (the ten required
regressions plus mid-run switch-back, per-tenant isolation, the worker
flag); suites that exercise the click itself enable LIVE explicitly
(`tests/execution/conftest.py`, `tests/hardening/conftest.py`,
`tests/desktop/test_real_submission.py`); `tests/conftest.py` resets to SAFE
around every test.

Small extras: a **Dry run complete — {company} — {role} — Ready for review**
notification (kind `DRY_RUN_COMPLETE`, one per run row, company/role only);
**target roles are editable** in Career Brain (`POST
/dashboard/profile/target-roles`, the existing tier1 / tier2 /
lower-priority / include-unrelated preference fields); the desktop
**Opportunities page shows match freshness** (decisions made with an older
gate ruleset, an older scoring engine) and a **Re-run matching** button on
the existing `POST /api/v3/matches/recalculate` (which re-syncs
opportunities as new recorded decisions; attempts, answers and past runs
are untouched). Nothing re-matches automatically.

## Desktop control center — Increment 1 (2026-09-12)

Not a blueprint phase: a thin local shell around the existing application.
`python -m app.desktop` (`app/desktop/`): readiness checks (migrated,
reachable database; `API_KEY` unless development — never generated), uvicorn
in a thread bound to `127.0.0.1` on `API_PORT` or a free port, `/health`
polling, an optional dry-run-only `LocalWorker` supervised in its own thread
(Playwright's sync API in a worker thread beside the API loop verified on
Windows), a pywebview / WebView2 window opened at a single-use
`/desktop/login?token=…` URL that sets the existing dashboard cookie (the
key never travels in a URL), `GET /desktop/status` (dashboard auth, flags
and counts), and shutdown in reverse order (window → worker and browser →
server). `API_HOST` now defaults to `127.0.0.1`. New optional extra
`desktop` (`pywebview`). Tests `tests/desktop/` (unit lifecycle + the real
entrypoint as a child process).

**Increment 2 (2026-09-12) — cookie-authenticated desktop writes.**
`require_api_key` (the single dependency on every `/api/*` write route)
accepts either the `X-API-Key` header (unchanged) or the dashboard cookie
together with the exact header `X-Requested-With: careeros-desktop`; the
header is CSRF / request-context protection, not a credential (checked
after the cookie matches the configured key in constant time); a cookie
alone never writes; `Origin` / `Sec-Fetch-Site` that name another site are
refused; CORS is unchanged (the header is not allowed from any origin);
reads, tenant resolution and endpoint authorization are untouched.
Tests `tests/desktop/test_desktop_auth.py`. Also fixed while running the
full suite: `signal_observations.observed_at` and
`signal_attributions.created_at` now default to `ordered_db_now` (the
convention audit and queue rows already use) — history written within one
Windows clock tick sorted arbitrarily and three signals tests were flaky
on an idle machine.

**Increment 3 (2026-09-12) — the control center.** `app/desktop/views.py`
+ `app/desktop/templates/` (Jinja2, vendored htmx 1.9.12 and
`static/desktop.js`, served under `/desktop/static`): a persistent sidebar
(Home, Opportunities, Applications, Attention, Documents, Career Brain,
Signals, Learning, Diagnostics, System) with the execution mode always
visible (SAFE / DRY RUN vs LIVE, kill switch). Pages are thin views over the
existing repositories and services; every write is a `fetch` to the
existing API route with the dashboard cookie and the desktop header (no key
anywhere in HTML or scripts). Only two backend touches: a `search`
substring filter on `OpportunityRepository.list_candidate_opportunities`
(also exposed as `?search=` on `GET /api/v1/opportunities/`) and a
cookie-authenticated inline file route `GET /desktop/documents/{id}/file`
for the candidate's own rendered documents. Real submission is not
reachable from the pages (Increment 4). Tests
`tests/desktop/test_control_center.py` (12: auth on every page, no key in
pages or scripts, home, opportunity search / filters / detail, application
groups and actions, review "why" and "what", NEEDS USER INPUT with an
answer box, foreign tenant invisible, attention center, documents and
inline file, system, cookie + header writes with the API-key path
unchanged).

**Increment 4 (2026-09-12) — user-controlled real submission.** A real
submission can now be made from the desktop, by the person, one application
at a time, and only from one screen. The review page shows two clearly
separate buttons for a READY attempt with a preparation — `[Dry run]` and a
red `[Submit for real]` — which both lead to the new confirmation screen
`GET /desktop/applications/{id}/confirm?mode=dry_run|live`
(`confirm_run.html`): company, role, attempt number, target URL with an
*Open in browser* link, the exact artifact type / version / SHA-256 that
would be uploaded (or "not rendered yet — rendering happens when execution
starts"), every prepared answer with ANSWERED / NEEDS USER INPUT and its
evidence grade, the mapped form fields when a snapshot exists, the
pre-submit preconditions, and the execution mode (HEADED PLAYWRIGHT — a
visible Chromium window on this machine; DRY RUN vs LIVE). `mode=live`
states in a red box that this will submit a real job application to the
employer and keeps the red button unusable until `SUBMIT` is typed into the
confirmation input (new `data-require-input` / `data-require-value` guard in
`static/desktop.js`; the form carries `onsubmit="return false"`). The button
posts `{"mode", "confirm"}` to `POST /desktop/api/attempts/{id}/run` with
the dashboard cookie and the desktop header (no key anywhere) and follows
the returned `job_url` (new `data-next-from`). The run supervisor
(`app/desktop/runner.py`) drives that one attempt through the existing
claim → precondition gate → `ExecutionService.execute` path with a headed
executor; the result page `GET /desktop/applications/{id}/run/{job_id}`
(`run_result.html`, polling the new partial
`GET /desktop/partials/run/{job_id}` → `_run_status.html` every 2s only
while the job runs) gives one headline — SUBMITTED / VERIFIED, SUBMITTED /
LIKELY, UNKNOWN / UNCERTAIN, BLOCKED, NEEDS REVIEW, DRY RUN COMPLETE,
RETRYABLE or PERMANENT FAILURE — and the full detail table (run id / number
/ executor / worker, attempt status, submit pressed yes/no, verification
status / method / detail, confirmation reference, application URL, the
signal the run created, the derived current outcome, timestamps). Nothing
is called a success without `VERIFIED` or a LIKELY verification; an UNKNOWN
outcome says explicitly that CareerOS will not resubmit it and offers the
existing confirm / reject buttons; a CAPTCHA, login or MFA is shown as a
handoff (what happened / why it needs you / what you can do) with a link to
the page. Tests `tests/desktop/test_run_pages.py` (8: running page and
partial, buttons hidden while a run is in progress, dry-run result, UNKNOWN,
handoff, permanent failure, a job that never started, tenant isolation) and
five more in `tests/desktop/test_control_center.py` (review buttons,
confirm page in both modes, a not-READY attempt, foreign tenant).
**Increment 5 (2026-09-13) — local desktop notifications.**
`app/desktop/notifications/`: notifications are *derived* from existing
rows, never stored. `events.py` reads, since a cursor, attempt transitions
in `application_events` (joined to `applications` for the tenant): READY →
Application Ready (only while still READY, and not when a retryable failure
just handed the attempt back from SUBMITTING), BLOCKED / NEEDS_USER_INPUT /
NEEDS_REVIEW / AWAITING_APPROVAL → Attention Required with the handoff
reason (CAPTCHA, login, MFA, unsupported / ambiguous form, unknown required
field, missing document, approval), SUBMITTED → Application Submitted,
VERIFIED / UNCERTAIN → Verification Result (UNCERTAIN says CareerOS will not
resubmit), FAILED → Execution Failed, INTERVIEWING / REJECTED → the signal
kinds; finished `execution_runs` that failed retryably (Execution Failed,
reason = error class, never the error text); and `signals` by category
(INTERVIEW_INVITATION, REJECTION, RECRUITER_CONTACT, ASSESSMENT,
INFORMATION_REQUEST; EXECUTION_RESULT / PLAYWRIGHT rows skipped; a repeated
observation bumps the same row and is not re-announced). Text is company,
role, status and reason words only; reasons must be bare vocabulary tokens.
`cursor.py`: one JSON file `DESKTOP_STATE_DIR/notifications.json` (new
setting, default `.cache/desktop/`, gitignored) holding per tenant and per
source a watermark plus the ids seen at that timestamp (ties are real:
`created_at` uses `db_now`), written atomically, fresh at *now* when missing
or corrupt — no table. `poller.py`: one thread under the existing
`WorkerSupervisor` (`careeros-notifier`, every 15 s, `--notification-interval`,
`--no-notifications`), one short read-only session per poll, stopped before
the worker and the server; `/desktop/status` reports its state and totals.
`center.py`: in-memory, per-tenant, bounded (200) list with seen flags —
sidebar badge, `GET /desktop/notifications` (+ partial polled every 30 s),
`POST /desktop/api/notifications/seen` (cookie + desktop header, memory
only, no database session). `native.py`: `winotify` toast on Windows
(`desktop` extra now lists `winotify` for win32; installed and verified
against the Windows toast history), `NullNotifier` elsewhere or with
`--no-native-notifications`; the adapter XML-escapes the payload and
neutralises PowerShell expansion because winotify escapes nothing (an `&`
in the click URL silently dropped the toast; `$env:USERNAME` in a body was
expanded). Click: `GET /desktop/open?to=<local path>&n=<single-use nonce>`
navigates the signed-in pywebview window (`window.navigate`, `load_url`
marshals to the GUI thread) or 303-redirects without a window; `to` must be
a bare `/desktop/` or `/dashboard/` path; no cookie is set; nothing runs.
Tests (5 files, 101): events (30), native adapter (15), center and pages (17),
poller / supervisor / open route (13), independent privacy review (26); the
child-process test now proves the poller runs and stops with the entrypoint.
Increment 6 (packaging / docs) is planned, not started.

**Controlled pilot validation and the Windows shortcut (2026-09-13).** The
existing desktop was run end to end on a scratch database (real Greenhouse,
Lever and Ashby boards discovered, matched, admitted and prepared to READY;
dry runs started from the review → confirm → run pages; no submission of any
kind). Bugs found on real pages and fixed, each with a regression test:
(1) `extension/src/discover.js` reported react-select's aria-hidden,
unfocusable "required" dummy input as a second copy of every Greenhouse
combobox (duplicate questions, doubled required counts) — such inputs are
skipped; (2) Lever wraps caption, control, typeahead results and upload status
in one `<label>`, so hint text ("No location found…", "Analyzing resume.")
became part of the question and answer-bank keys could never match — the
caption element wins and status / hint / error text is stripped; (3) a
recorded profile location was not used for location questions
("Location (City)", "Current location") — `app/execution/forms.py` answers the
LOCATION category from the profile fact before asking; (4) a WHY_COMPANY /
WHY_ROLE answer-bank entry written for one employer was reused for another
employer's question by category alone — those categories now need the exact
question or the tailored preparation; (5) a NEEDS_USER_INPUT run was
headlined NEEDS REVIEW — it has its own headline naming the reason; (6) a
handoff that stopped before the form still read "Submit pressed: yes"
(`submit_invoked` is recorded before the executor may click) — the result
page derives what was actually pressed; (7) a READY attempt whose SUBMIT item
had been parked (stale preparation → re-prepared) failed from the desktop
with a misleading "lease held" — the person's click re-opens the parked item
through the existing `retry` path (same attempt, one execution) and claim
refusals name the real state; (8) `verify_schema()` advised
`alembic stamp head` for tables without a revision, which wedges the database
— it now tells the operator to stamp the *matching* revision, and the desktop
readiness check says the same; (9) launching the desktop while it was already
open started a second server, window, worker (same worker id) and
notification poller — `app/desktop/instance.py` holds one OS-level lock per
`DESKTOP_STATE_DIR` and a second launch exits with code 3. New: `python -m
app.desktop --install-shortcut` (`app/desktop/shortcut.py`, icon
`app/desktop/static/careeros.ico`) writes `CareerOS.lnk` to the Desktop
(pythonw + `-m app.desktop --worker --headed`, repository as working
directory, no credential; idempotent), and a console-less start logs to
`DESKTOP_STATE_DIR/desktop.log` and shows startup problems in a message box.
Still unproven: a real employer submission (never attempted), a complete
fill-to-final-page dry run on a real ATS page (every sampled posting needs
answers only the candidate can give: country, EEO, LinkedIn, "why us",
employer-history questions).

**First real-data run (2026-09-13).** The real `careeros.db` (Alembic head,
integrity ok) holds the Evidence Graph imported from `data/career_seed.json`
on first start (46 nodes, all `SEED_FILE` provenance; 12 of 13 skills
VERIFIED; profile email, phone, work authorization, LinkedIn and GitHub
empty; answer bank empty). Normal discovery over Stripe, Rubrik, Notion,
Replit and Perplexity (1,083 jobs, 0 failures), matching, sync and one
scheduler pass under the existing policy. Bugs found and fixed, each with a
regression test: (1) the eligibility engine gated only graduation year, work
authorization and location, so "Senior Software Engineer" and "4+ years of
professional experience" roles were LIKELY_ELIGIBLE for a student and
admission chose them over internships (the 90-day company cooldown then
locked each company to the wrong role) — a pre-career candidate (not yet
graduated, no recorded employment) is now INELIGIBLE for senior-level roles
or 2+ required years in the qualifications and UNCERTAIN for years mentioned
only in free text or mid-level roles; experienced or graduated candidates
are unaffected; (2) a "(US Citizen)" title requirement was not detected — it
is now a work-authorization gate decided by the recorded status; (3) form
mapping filled Replit's required "Project URL" and "Project Password" with
the relevant-project paragraph by category — link fields accept only an
exact-question answer that is itself a link and password fields are never
filled; (4) the desktop Review page said "answer below" but offered no box
for the preparation's open questions — it now has an answer box per
question (existing preparation answer route, cookie + desktop header), a
save-to-bank option for reusable categories, an "employer-specific" note for
why-company / why-role questions, and a link to the preparation page. The
three wrongly admitted senior attempts were released through the existing
cancel path. Test posting: Replit, Software Engineer - New Grad (2027),
Ashby (LIKELY, fit 69 MEDIUM, priority 68, admitted, preparation
NEEDS_USER_INPUT). Remaining blocker: candidate-only information (email,
phone, work authorization, sponsorship, notice period, salary expectation,
the Replit project URL and its password if any); no dry run could start
without it and nothing was submitted. Known, not fixed: fit scoring gives
unrelated roles high fit (Perplexity "Motion Designer" 69, Notion "Data
Scientist, Growth" 88); Stripe postings resolve to stripe.com search pages.

**Candidate profile replaced with the real candidate's resume (2026-09-13).**
The imported seed (`data/career_seed.json`) described a different person.
The real candidate's resume was converted into `data/local/career_seed.rithik.json`
(gitignored, personal data; the PDF is kept beside it) and imported with the
existing `SeedImporter`: every active evidence node now carries that file as
provenance; the previous seed's nodes were soft-removed with an audit trail
(backup: `backups/careeros-before-rithik-profile-2026-09-13.db`). Treatment:
skills used by a project or the internship on the resume are VERIFIED, skills
only listed are UNVERIFIED; project numbers are UNVERIFIED metrics
(candidate-reported); city, work authorization and backlogs are left empty
(not on the resume); preferences are initial values derived from the roles on
the resume and flagged for the candidate to confirm. The eligibility
experience gate now treats a not-yet-graduated candidate with under 12 months
of dated employment as pre-career (the 4-month internship had switched it
off); undated employment is never assumed short. Preparations built from the
old profile were invalidated and rebuilt.

**Geographic targeting (2026-09-14).** The candidate is in Hyderabad,
Telangana, India, but the Opportunities screen was mostly US postings. Root
cause: the five boards were US companies. Greenhouse and Ashby public APIs have
no location filter, and Lever's `location=` matches only a board's exact
strings. Locations were opaque strings, the eligibility location gate fired
only for postings extracted as ON_SITE, the location preference was empty, and
admission had no geography rule, so US hybrid/remote roles were LIKELY and held
company cool-down slots. Fix: `app/jobs/geography.py` (compact gazetteer with
metro aliases, `parse_places`, `GeographyPolicy` → PRIMARY / COUNTRY_REMOTE /
COUNTRY_OTHER / FOREIGN / FOREIGN_REMOTE / REMOTE_UNSPECIFIED / UNKNOWN, office
names only as a fallback). Configuration lives in Career Brain preferences:
`location_primary` (empty = profile location), `location_include_country_remote`
(default on), `location_include_other_cities` (off), `location_allow_international`
(off), `location_include_unconfirmed` (on), `preferred_locations` = also consider;
card on `/dashboard/profile/`. Propagation: discovery drops postings tied only
to countries no projected tenant targets (counted in `jobs_filtered` and
`checkpoint.filtered_geography`; known postings stay open); normalization adds
Ashby/Lever secondary locations and `extraction_metadata.geography`;
eligibility compares every work mode with the recorded location (abroad /
US remote / other Indian city / "Remote" only → UNCERTAIN, never inferred
authorization); a "work in <country>" requirement is checked against the
countries the recorded authorization names; the preference location signal
scores against the target; Tier-1 gate `GEOGRAPHY` (`OUTSIDE_TARGET_GEOGRAPHY`,
ruleset `tier1-gates-v2`) in sync and the scheduler. Also fixed from the real
run: unrelated-title unread JDs capped at fit 40 and any unread JD at 60, the
scheduler orders by fit band before priority (a fresh LOW role took a MEDIUM
role's company slot), "(9 - 12 Years)" / "8+ years" mentions → UNCERTAIN, full
US state list. Real run: 25 India-hiring boards probed live and added; 2,030
active jobs → Hyderabad 126, India remote 14, other India 874, US 629, other
international 363, unconfirmed 24; attempts outside the preference released via
`ExecutionService.cancel` (backup `backups/careeros-before-geography-2026-09-14.db`).
Open: many admitted Hyderabad roles are non-technical LOW-band roles (the LOW
band is enabled by the candidate policy); fit still over-rates thin JDs;
work authorization, notice period and salary remain candidate input.

**Selection quality (2026-09-14).** With the right market, the queue still held
Affiliate Marketing, Math Video Creator, Product Support and a talent community.
Root causes: no job-side role family (only title-token overlap worth part of
the 20% preference weight), a fit engine that scored a two-keyword match as a
100% technical match and an unread description on location + eligibility alone,
a years reader that needed the literal word "experience" ("(9 - 12 Years)",
"5+ years in ML systems" missed) while a separate content regex marked roles
SENIOR without saying why, scheduler order that let a fresh weak role take a
company's slot, and `ExecutionService.cancel` leaving the PREPARE item open.
Fix: `app/intelligence/services/role_relevance.py` (title-head role families:
technical / potentially technical / non-technical, description fallback,
relevance against the candidate's own target families: RELEVANT / ADJACENT /
UNKNOWN / WEAK / UNRELATED); `app/intelligence/extraction/seniority.py`
(`experience_statements` with quoted snippets, preferred flag, company-age
exclusions; `title_seniority`); fit caps UNRELATED 30, WEAK 55, unread JD 60
and a neutral prior on the technical component; experience gate: senior title
blocks, 2+ years in qualifications / 3+ years with "experience" / 5+ year bands
block, preferred or smaller → UNCERTAIN; Tier-1 gate ROLE_RELEVANCE
(`IRRELEVANT_ROLE`, `tier1-gates-v3`, preference `role_include_unrelated`);
scheduler order eligibility → relevance → band → priority; cancel closes the
PREPARE item. Real run (unconfirmed locations switched off as the candidate
asked): 1,951 active; 8 admitted, 5 queued, all MEDIUM, 5 technical + 3
potentially technical; queue cleaned through `ExecutionService.cancel` (backup
`backups/careeros-before-selection-quality-2026-09-14.db`).

**First real dry run complete (2026-09-14).** Notion — Software Engineer,
Developer Experience (Hyderabad, ELIGIBLE, Ashby) went QUEUED → prepared →
READY → Review → headed desktop DRY RUN: 15 fields discovered, 6 filled (name,
email, phone, LinkedIn URL, resume upload ×2), 9 skipped, invisible reCAPTCHA
only, "Submit Application" located and not pressed (`submit_invoked` 0; SUBMIT
item NEEDS_REVIEW "dry run complete"). **Correction (audit, same day):** that
run's "0 required missing" was wrong — discovery did not see three required
Ashby questions (the Location combobox and two Yes/No button questions: office
"Anchor Days" and sponsorship). Discovery now reports them as unoperable
required fields, and a dry run on a copy of the database hands off with those
three questions for the person to answer on the employer page. Bugs fixed on
the way, each with a regression test: queue claims ELIGIBLE work before
UNCERTAIN work with a stale higher stored priority; answering the last question
on the Review page left the PREPARE item BLOCKED so the attempt never reached
READY (scheduler reconcile now resolves it); the queue worker rebuilt a package
prepared with the employer form's own questions with the standard question set;
`NUMBER_PATTERN` read "SQLAlchemy 2," as "2," and dropped a truthful block;
profile link values ticked a "How did you hear about us?" checkbox. The four
other queued attempts wait on candidate-only facts (work authorization,
sponsorship, notice period, salary; Atlan also asks most recent employer).

**Functional audit + desktop UI redesign (2026-09-14).** Four parallel audits
(backend services, execution/desktop runtime, UI, security/performance) with
an integration pass. Backend: Lever descriptions now include `lists` /
`additional` and Greenhouse content is unescaped before extraction (requirements
were invisible to eligibility); graduation lists / month-season phrasing;
negated authorization, clearance and sponsorship statements; study durations
not experience; unrecognised primary city keeps geography on; partial
preferences PUT merges; employer-specific answers reused only for the named
employer; orphaned rendered documents no longer wedge a preparation; profile
value validation; queue items with exhausted attempts park on lease expiry.
Execution: discovery reports Ashby / Greenhouse comboboxes and Yes/No button
questions as unoperable required fields (hand off, never silently skipped),
Lever ✱ markers, Ashby "Autofill from resume" is not the resume upload, a form
appearing after a cleared wall is never submitted unmapped. Security: Host
allowlist against DNS rebinding, cross-site write refusal for cookie-authorised
dashboard/desktop forms, uvicorn access-log redaction, percent-escape /
backslash redirect hardening, route-guard sweeps that actually walk the routes.
Performance: opportunity pages eager-load (61 → 12 queries), target-role
classification cached for scheduler preview (~2.9 s → ~2 s). UI: shared
`app/desktop/static/careeros.css` design system, permanent safety banner,
workflow navigation, command-center Home, filterable paginated Opportunities
with plain-language relevance/location, grouped Applications, pre-flight Review
checklist derived from stored state, Attention task inbox, Documents library,
restructured Career Brain; dashboard htmx served locally.

## Completed in Blueprint Phase 13 (2026-09-12) — real-world validation

No new subsystem. The pipeline was run end to end against real public
boards and application pages, in dry run only, and what the real world
broke was fixed with reusable changes. Full write-up with the raw
observations: `docs/validation/phase13/REPORT.md` (+ `*.json`); regenerate
with `tools/validation/phase13_validate.py <stage>` (scratch database under
`%TEMP%/careeros_phase13`, AI off, one page at a time, `execute` never
called on an employer page, nothing submitted).

- **Observed on real boards** (Greenhouse discord / duolingo / asana, Lever
  nium / spotify, Ashby linear / posthog / supabase): 430 postings → 429
  jobs → 409 opportunities, 0 rejects, 0 AI calls, a second pass 100 %
  unchanged; 429 matched in 18 s; 40 prepared to READY in 2.3 s with 42
  rendered PDFs that all open, hash-match and carry the candidate's name.
- **Observed on real application pages** (final pass, 21 pages): every
  page with a reachable form is discovered and fully mapped — Greenhouse
  hosted (34 fields), Greenhouse embedded in the employer's site (39 fields
  inside the iframe), Lever `/apply` (15–17 fields; its hCaptcha runs in
  invisible mode and is reported, not a wall), Ashby (12–14 fields after a 0.5 s
  client-render settle); the only non-discoveries are two PostHog postings
  whose board renders no form (`AMBIGUOUS_FORM`). Special pages (reCAPTCHA
  and hCaptcha demos, GitHub / LinkedIn login, board indexes, a missing
  posting, `about:blank`) all end in the intended handoff.
- **Fixed (reusable, none site-specific):** Lever adapter reads the live
  payload shape (`hostedUrl` / `applyUrl`; the form is only on `/apply`);
  URLs are part of the raw hash and a same-source job follows a moved apply
  link; `PlaywrightExecutor._settled_scan` re-scans main and child frames
  for up to `PLAYWRIGHT_SETTLE_MS` (8 s) and carries the frame that holds
  the form through fill / submit / rescan (`RunState.target`,
  diagnostics `in_frame` / `settle_ms`); a page whose only form is a search
  box or cookie banner is an `AMBIGUOUS_FORM` handoff
  (`_looks_like_application_form`, submit-looking buttons only); discovery
  reports `captcha_visible` / `captcha_inline` / `captcha_solved` (the
  provider's response token — detected, never produced) so an invisible
  reCAPTCHA badge no longer blocks a run and an inline widget a person
  completed no longer blocks the Playwright run or the extension's Fill
  (a visible widget or challenge text still hands off before typing); upload
  controls map on name / id as well as label ("Attach" → `resume`,
  `cover_letter`); re-captured forms join answers by position (two "Attach"
  rows no longer share one answer); attribution folds titles like the
  searched text (real titles carry dashes and commas — 10/13 review-queue
  signals attributed on reprocess, 1/13 before); dev-server docs bind to
  `127.0.0.1`.
- **Proven by tests:** `tests/execution/test_playwright_failures.py` (10
  browser failure modes over loopback HTTP with route injection: slow, hung,
  5xx, reset, tab closed mid-fill / after the click, navigation and DOM
  change mid-fill, reload, late confirmation — before the click everything
  is retryable with 0 clicks, after it UNKNOWN and never resubmitted), new
  fixtures `embed_host.html`, `spa.html`, `search_only.html`,
  `banner_only.html`, `captcha.html?solved=1`, and 12 new unit / flow tests
  for the fixes above.
- **Signals / learning with realistic data:** 16 message shapes (Greenhouse,
  Lever, recruiter, HackerRank, Calendly, LinkedIn alert, HTML-only,
  threaded reply) classified 14/14, duplicate → observation, company-only
  with two live applications → AMBIGUOUS; learning snapshot over 12 rows
  reports "insufficient data" everywhere (no methodology change).
- **Data chain:** for sampled attempts *why selected → what would be
  submitted → evidence → afterwards* reconstructs from stored rows alone.
- **Not yet proven:** live submission on a real employer form; the
  extension inside a real Chrome (service-worker restart, runtime sender
  validation, tab navigation / refresh / SPA route changes, different-origin
  confirmation) — page scripts were exercised on real pages through
  Playwright injection only; the extension content script is top-frame only
  (Greenhouse embeds unseen until `all_frames` injection, P2); multi-day
  operation; PostgreSQL for the new paths (CI).

## Completed in Blueprint Phase 12 (2026-09-19)

- **Concurrency audit** (`tests/hardening/test_concurrency.py`): real threads
  with their own sessions on SQLite (WAL + retry on lock) for queue claims,
  cap reservations, parallel email delivery and parallel learning
  snapshots; deterministic interleavings for two schedulers on one
  opportunity, two executors on one item, duplicate results and
  verifications, concurrent human confirmations. Findings fixed:
  `signals.observation_count` was a read-modify-write (lost update under
  parallel delivery) → atomic `UPDATE … + 1`; `PolicyRepository.get_or_create`
  raced on the first policy insert → savepoint + re-read.
- **Execution safety** (`test_execution_safety.py`): `submit_invoked` durable
  before the click; UNKNOWN never resubmits; **expired lease refused at the
  gate** (new `LEASE_EXPIRED` failure; heartbeat renews); Playwright vs
  extension on one attempt → exactly one click, the loser refused on gate /
  heartbeat / result; an armed-then-silent extension → UNCERTAIN, never a
  second click; kill switch at queued / claimed / form / armed stages and
  on retry; blocklist, closure, stale preparation revalidated at submit.
- **Startup recovery** (`app/execution/recovery.py`,
  `ExecutionService.recover_lost_runs`, wired into the lifespan and every
  `run_queue`): RUNNING runs whose lease lapsed are settled — after submit
  → UNKNOWN / UNCERTAIN / NEEDS_REVIEW + signal; before submit →
  FAILED_RETRYABLE / READY / PENDING; live leases untouched; idempotent;
  never raises at startup (`test_recovery.py`).
- **Security** (`test_security.py`): IDOR sweep over attempts, runs, form
  fields, queue items, preparations, documents (+ file), signals (+ trace),
  outcomes, learning snapshots, candidate opportunities and the legacy v5
  routes — every foreign id is 404/405/409/422; **the legacy
  `/api/v5/applications` list/get routes were not tenant-filtered and now
  are**; every `/api/*` write route requires the API key and every
  `/dashboard/*` route requires dashboard auth (asserted over the live
  route table); traversal / absolute paths refused; hash mutation
  detected; redaction of keys, cookies, JWTs, Gemini keys; CORS never
  wildcard; extension manifest permissions pinned and **message listeners
  now check `sender.id === chrome.runtime.id`**; exactly one AI boundary;
  no string-formatted SQL; config endpoints carry no secrets.
- **Caps** (`test_caps_hardening.py`): cap 0 / 1 / boundary, lowering after
  reservations keeps them and never goes negative, raising admits more,
  timezone rollover opens a new period, closed openings release, permanent
  failure / cancellation release while retry / handoff / unknown keep.
- **Failure injection** (`test_failure_injection.py`): prepare crash →
  retryable, crash after submit → UNKNOWN, verification crash → SUBMITTED
  with UNKNOWN verification (never VERIFIED), corrupt artifact → replaced by
  a fresh verified version before upload, DB error in signal emission never
  breaks execution, an audit-write failure fails closed and recovery then
  settles the run without a second click, policy first-touch race.
- **Diagnostics** (`/api/v1/ops/diagnostics`, `/dashboard/ops`,
  `app/api/routes/diagnostics.py`): queue depth and stale leases, attempts
  and runs by status, 24 h execution throughput, signals awaiting review,
  discovery failures, AI counters, cap utilisation, kill switches, learning
  snapshot age, warnings; counts only; tenant-scoped.
- **Migrations** (`test_migrations_chain.py`): single head `e4b2d8f0a6c3`,
  fresh → −1 → head → base → head round trips with ORM parity, critical
  constraints and indexes asserted. **Backup/restore**
  (`test_backup_restore.py`): a synthetic database copy restores every row,
  keeps constraints, and startup recovery leaves UNKNOWN as UNKNOWN.
- **Docs**: `docs/OPERATIONS.md` (invariants, execution safety rules,
  recovery, health, backup, retention, index review, SQLite/PostgreSQL
  parity, security summary).
- **Benchmarks (2026-09-19, one sequential 55-minute battery on the
  laptop, SQLite, AI calls 0 everywhere)**: discovery 10,000 synthetic jobs
  — first pass 49 s (10,958 jobs/min, 57,107 writes, 9,026 persisted, 8,595
  opportunities, 801 source duplicates, 173 malformed rejected), second
  pass 250 s (8,880 unchanged, 913 versioned, 1 repost, peak 38 MB);
  scheduler 5,001 candidate opportunities — 331 s first run (3,000 admitted
  = every admissible one; 250 each of six refusal reasons), 25.7 s second
  run, peak 65 MB; preparation 5,000 — L0 815 s (368/min), L1 865 s, 35,000
  writes each, peak 112 / 130 MB; execution 1,000 mock attempts — 641 s
  (94/min), 600 submit calls each exactly once, 500 VERIFIED / 101
  SUBMITTED / 50 UNCERTAIN / 150 BLOCKED, 34,352 writes, peak 44 MB — this
  exceeded the module's 600 s laptop sanity bound by 7 %; signals 5,000 —
  36.5 s (137/s); learning 10,000 — dataset 3.5 s, aggregation 1.1 s,
  snapshot 4.8 s. **Soak** (3 rounds × 30 opportunities through scheduler →
  preparation → mock execution with PDF rendering → duplicate email
  delivery → learning snapshot): 28–31 s per round, 30 admitted and 21
  submitted each round, 0 stale leases, 0 RUNNING runs, every attempt
  clicked once, duplicate deliveries never became signals, Python heap
  17.4 → 18.8 → 19.1 MB (flat). Compared with the Phase 5 / 8b / 10
  numbers, the scheduler first run (185 → 331 s), preparation (≈101 →
  163 s per 1,000) and signals (175 → 137/s) were 1.3–1.8× slower inside
  the battery while learning was unchanged; none of those code paths
  changed in Phase 12. Isolated re-runs afterwards: scheduler 501 in 12.2 s
  first / 0.8 s second (Phase 5: 37 s), preparation 1,000 L0 49 s / L1 53 s
  (Phase 4: 101 / 71 s), signals 1,000 in 6.5 s (155/s, identical to Phase
  10), execution 1,000 in 599 s (100/min, inside the bound) — so the
  battery slowdowns were the machine, not the code. Regression: 931 passed
  / 2 skipped (PostgreSQL-only), ruff clean.

## Completed in Blueprint Phase 11 (2026-09-18)

- **Learning objective and boundary** (`app/learning/`): learning produces
  *signals* (observed rates, statistics, snapshots, recommendations) for
  ordering and information. It never writes a policy, cap, band, threshold,
  blocklist, eligibility rule, the Evidence Graph, a preparation or an
  execution gate; the only behavioural hook is the pre-existing
  `learned_prior` priority component, fed only when the candidate turns
  `learning_settings.ordering_enabled` on (default off). A test asserts the
  engine's source never references the policy's volume fields.
- **Dataset** (`dataset.py`, `features-v1`): one `LearningRow` per executed
  application at `as_of`: opportunity / company key / normalized title /
  positioning role family / fit band + score / admission policy + gate
  ruleset versions / source / preparation lane, level, cover-letter mode,
  variant / execution method + verification / labels (responded,
  interviewed, rejected, assessed, withdrawn), days to response and to
  rejection, `evidence_quality` (strongest counted evidence),
  `evidence_counts`, `events_below_threshold`, `outcome_completeness`.
  **No leakage**: only attempts created and executed by `as_of`, only
  events with `observed_at <= as_of` (knowledge time, not employer time),
  retracted / superseded events never; optional rolling `window_days`.
  **Evidence**: labels come only from events at or above
  `minimum_evidence` (MODERATE default; WEAK is exploratory); UNKNOWN /
  LIKELY verification is never `verified`.
- **Statistics** (`stats.py`, `beta-binomial+wilson-v1`): smoothed rate =
  `(positives + baseline × k) / (n + k)` with `k = prior_strength` (10)
  toward the tenant's own baseline (the baseline itself is not shrunk);
  95 % Wilson intervals; confidence NONE / LOW (< `min_samples`) / MEDIUM
  (< 4×) / HIGH; medians for durations.
- **Engine** (`engine.py`, `learning-v1`): `aggregate()` per dimension
  (SOURCE, COMPANY, TITLE, ROLE_FAMILY, FIT_BAND, LANE, TAILORING_LEVEL,
  COVER_LETTER_MODE, POSITIONING_VARIANT, EXECUTION_METHOD) with response /
  interview / rejection / assessment / verified-submission / uncertainty /
  review rates and median days to response / rejection, evidence mix and
  confidence; `source_discovery()` (shared job-side closure / duplicate /
  valid-job / run-failure rates, information only); `recommendations()`
  (HIGHER_/LOWER_RESPONSE, HIGHER_/LOWER_INTERVIEW only when the Wilson
  interval clears the baseline at ≥ MEDIUM confidence; HIGHER_UNCERTAINTY
  for execution methods with the routing caveat; INSUFFICIENT_DATA for
  sparse groups; HIGH_CLOSURE/DUPLICATE_RATE for sources), every one with
  metric, n, positives, observed and smoothed rate, interval, baseline,
  confidence, evidence mix, versions, window and a non-causal caveat;
  `snapshot()` persists `learning_snapshots` + `learning_metrics` +
  `learning_recommendations` (never updated; audit event `learning_snapshot
  / generated`); `expected_response()` = 50 + 100 × confidence-weighted
  mean of (smoothed − baseline) over response and interview rates for
  source, company, title, fit band; neutral (50) without data.
- **Priority integration** (`app/pipeline/sync.py`, `learned-ordering-v1`):
  `sync_run` builds one `LearnedPrior` per run when ordering is enabled;
  `sync_match` passes its score as `learned_prior` (else `None` = neutral,
  exactly as before), records the snapshot id / versions / explanation in
  the priority components, and re-records when the learned component
  changed. Weights are untouched (`learned_prior` stays 0.05 by default).
- **Settings**: `policy.learning_settings` (`TenantLearningSettings`:
  `ordering_enabled`, `window_days`, `min_samples`, `prior_strength`,
  `minimum_evidence`), versioned and audited as a policy update; migration
  `e4b2d8f0a6c3`.
- **API + dashboard**: `/api/v1/learning/{settings, compute, snapshots,
  snapshots/latest, snapshots/{id}, recommendations, dataset,
  expected/{co}, source-discovery}`; `/dashboard/learning` (totals,
  breakdowns with n / confidence / smoothed rate / interval / evidence,
  recommendations, source discovery, settings, snapshot button).
- **Tests**: `tests/learning/` — statistics (empty / one / sparse / normal
  / large / smoothing / intervals / confidence), dataset (joins, evidence
  weighting, UNKNOWN vs LIKELY vs VERIFIED, no future leakage incl.
  observed-later events, window, retracted events, tenant isolation,
  empty), learning (per-dimension metrics and counts, response times,
  sparse groups not authoritative, associational recommendations with
  evidence, immutable versioned snapshots, methodology change = new
  version, window on snapshot, job-side source discovery), safety (policy /
  caps / bands / blocklist / evidence untouched, scheduler admits every
  admissible opportunity with ordering on, priority unchanged unless
  enabled and back to default when disabled, bounded neutral signal, no
  hidden top-N, cross-tenant isolation incl. API), API + dashboard;
  benchmarks in `tests/learning/test_perf.py`.
- **Benchmarks (2026-09-18, SQLite, AI calls 0, gateway requests 0)**:
  1,000 executed applications — dataset 1.0 s, aggregation 0.3 s (73
  groups, peak 0.9 MB), recommendations < 0.01 s (63), snapshot persisted in
  1.4 s (666 metric rows); 5,000 — dataset 4.7 s, aggregation 1.6 s (273
  groups, 3.6 MB), snapshot 6.3 s (2,466 metric rows, 283
  recommendations); 10,000 — dataset 3.1 s, aggregation 1.0 s (523 groups,
  6.8 MB), snapshot 4.2 s (4,716 metric rows, 558 recommendations). Full
  regression 872 passed / 2 skipped (PostgreSQL-only), ruff clean.

## Completed in Blueprint Phase 10 (2026-09-17)

- **Signal model** (`app/signals/models.py`, `database/models.py`, migration
  `d3a1c7e9f5b2`): `signals` (tenant, source, `source_reference`,
  `content_hash`, `dedupe_key` unique per tenant, subject, sender +
  domain, bounded redacted `excerpt`, sanitized `payload` incl. hints /
  references / URLs, `external_at` vs `observed_at`, observation count,
  classification fields + `classifier_version`, status + reason,
  attribution fields, links to application / opportunity / candidate
  opportunity / execution run, AI metadata, `schema_version`),
  `signal_observations` (every delivery), `signal_attributions`
  (append-only, superseded never deleted), `outcome_events` (append-only,
  evidence strength, origin, `event_at` + `time_basis`, sequence,
  `dedupe_key` per signal/application/outcome/origin, superseded /
  retracted flags), `application_outcomes` (derived current + provisional
  status, conflicts, `rules_version`, version). Versions:
  `signals-v1`, `signal-classifier-v1`, `signal-attribution-v1`,
  `outcome-rules-v1`.
- **Ingestion** (`service.py:SignalIngestionService.ingest / ingest_email /
  ingest_many`, `normalize.py`): idempotent per tenant on the source
  reference (message id, run + verification state, page observation id)
  or, failing that, the normalized content hash; a repeat adds an
  observation with provenance (`content_changed` when the text differs);
  HTML stripped, quoted replies and signatures cut, credential-shaped
  fragments redacted, excerpt bounded by `SIGNAL_EXCERPT_CHARS`; only
  `In-Reply-To` / `References` / `Date` / `List-Unsubscribe` are read from
  headers; `EmailMessage` is the connector boundary (no mailbox integration).
- **Classification** (`classify.py`): named regex rules with a fixed
  category precedence and HIGH / MEDIUM / LOW / NONE confidence;
  incompatible co-matches downgrade to MEDIUM; marketing senders → OTHER
  (auto-IGNORED); execution results classify from their verification
  status; MANUAL signals may declare a category. Optional AI (`ai.py`,
  `AIOperation.CLASSIFY_SIGNAL`, candidate scope, `classify-signal-v1`)
  only when rules are not confident and the tenant's AI is on; the model
  may only pick a category from the enum, is capped at MEDIUM, and is
  recorded as `classification_source=ai` (WEAK evidence). Unavailable /
  malformed / budget / refused → NEEDS_REVIEW.
- **Attribution** (`attribution.py`): strong identifiers first
  (application id, execution run, candidate opportunity, opportunity,
  source job), then the email thread, confirmation / reference numbers
  matched against `external_application_id` / `confirmation`, embedded
  ids, application URLs (normalized, prefix either way); textual company
  (+ title) evidence last and only for executed attempts; exactly one
  application → MATCHED (HIGH, or MEDIUM for company-only), several →
  AMBIGUOUS, disagreeing identifiers → `conflicting_identifiers`, nothing
  → UNMATCHED. Every decision stores rule, evidence, candidates,
  explanation, version and actor.
- **Outcomes** (`outcomes.py`): category → outcome mapping; evidence
  STRONG (deterministic HIGH, VERIFIED execution evidence, declared or
  human) / MODERATE (MEDIUM, LIKELY) / WEAK (AI, LOW, UNKNOWN);
  `derive()`: highest stage reached (not the latest event), terminal
  outcomes need STRONG, weak evidence is provisional and never downgrades,
  progress after a terminal event or two different terminals →
  NEEDS_REVIEW with the conflicts listed, failed verification flags review.
  Lifecycle sync is guarded and limited to → INTERVIEWING and → REJECTED;
  WITHDRAWN / CLOSED_WITHOUT_APPLICATION stay outcomes for a person.
- **Execution integration** (`execution.py`, `ExecutionService._emit_signal`):
  every `_apply_verification` and every UNKNOWN / unverified SUBMITTED
  result records a signal (source EXECUTION / PLAYWRIGHT / EXTENSION,
  reference `execution_run:{id}:{verification}:{method}`) — same result
  again = observation, an upgrade = new signal; VERIFIED → STRONG
  SUBMITTED, LIKELY → MODERATE, UNKNOWN → WEAK (never upgraded), FAILED →
  NEEDS_REVIEW. A STRONG confirmation signal for a SUBMITTED / UNCERTAIN
  attempt verifies it through `ExecutionService.verify` with
  `VerificationMethod.CONFIRMATION_EMAIL` (no second mechanism). Inbox
  failures never break execution (savepoint + warning).
- **Review + API + dashboard**: `link`, `confirm_classification`,
  `reject_classification`, `confirm_outcome`, `ignore`, `merge`,
  `reprocess`, `purge_excerpts` — every action audited on the `signal`
  entity, earlier attributions superseded, earlier events retracted with a
  reason. `/api/v1/signals` (ingest / batch / email, list with status /
  source / category / application / outcome / company / days / review
  filters, review queue, summary, outcomes list + per-application history,
  get, trace, classify / attribute, link, confirm-classification,
  reject-classification, confirm-outcome, ignore, merge, purge-excerpts).
  `/dashboard/signals` (inbox with filters and outcome counts) and
  `/dashboard/signals/{id}` (trace signal → attribution → opportunity →
  candidate opportunity → preparation → execution runs → application
  history, actions, event and attribution history, observations).
- **Settings**: `SIGNAL_EXCERPT_CHARS` (2000), `SIGNAL_EXCERPT_RETENTION_DAYS`
  (180; `purge_excerpts` keeps hashes, classification, attribution, outcomes).
- **Tests**: `tests/signals/` — ingestion (unique / duplicate / replay /
  hash / source identity / tenant isolation / malformed / redaction /
  bounded excerpt / headers), classification (13 categories, determinism,
  incompatible pairs, marketing, AI disabled / not consulted / ambiguous /
  malformed / unavailable / budget / hosted refusal / tenant switch),
  attribution (each rule, ambiguity, conflict, unknown id, thread,
  pre-execution exclusion, tenant isolation, append-only history), outcomes
  (pure rules incl. out-of-order, conflicts, weak evidence, retraction;
  persisted history, duplicate deliveries, conflicts, provisional →
  confirmed, time semantics, withdrawal/closure), execution integration
  (VERIFIED / UNKNOWN → confirm / LIKELY vs reference / extension and
  Playwright sources / duplicate reports / failed verification /
  confirmation email verifies / inbox failure isolation), review + API +
  dashboard (audit trail, merge, purge, API flow, tenant scope, pages),
  truth boundary (signals and AI never create evidence; weak AI is
  provisional; preparation fingerprint untouched); benchmarks in
  `tests/signals/test_perf.py`.
- **Benchmarks (2026-09-17, SQLite, AI disabled: 0 AI calls, 0 gateway
  requests everywhere)**: 1,000 emails against 200 executed attempts
  ingested + classified + attributed + derived in 6.4 s (155/s; rows
  written: 1,000 signals, 1,000 observations, 875 attributions, 750 outcome
  events, 150 application outcomes; 750 applied / 125 needs review / 125
  ignored); 5,000 in 28.6 s (175/s; 3,750 events); duplicate-heavy 300
  signals delivered 4× = 1,200 observations but 300 signals and 226 events
  (43.6 s under tracemalloc, Python peak 0.47 MB); components in isolation:
  classification 2,857/s, attribution 13,822/s (1,000/1,000 matched),
  derivation 5,432/s over 20-event histories. Full regression 843 passed /
  2 skipped (PostgreSQL-only), ruff clean.

## Completed in Blueprint Phase 8b (2026-09-16)

- **Tier-1 gate set** (`app/pipeline/gates.py`): `GATE_RULESET_VERSION =
  "tier1-gates-v1"`; `Gate` enum; `GATE_CATALOG` in evaluation order
  (CANDIDATE_SKIP → OPENING_OPEN → EVALUATED → HARD_ELIGIBILITY →
  MINIMUM_ELIGIBILITY → COMPANY_BLOCKLIST → FIT_SCORED → BAND_ENABLED →
  FIT_FLOOR, plus the stateful NOT_ALREADY_SUBMITTED / NO_DUPLICATE_APPLICATION /
  CANDIDATE_INFO_COMPLETE / EVIDENCE_RESOLVED / APPLICATION_SUPPORTED
  catalogued with where they are enforced), each mapped onto an existing
  `AdmissionReason`; `evaluate_gates()` is pure, runs every static gate and
  returns a `GateReport` (ruleset version, policy version, per-gate
  passed/code/detail, first failing gate decides); `evaluate_admission()`
  now derives from it with the Phase 2/5 reason strings unchanged and
  carries the report; the scheduler's `Verdict` carries it too. Priority is
  not an input; nothing limits the count. `GET /api/v1/policy/gates`
  returns the catalog with the current parameter values; the policy page
  shows it.
- **Fit bands as versioned settings**: `FitBandConfig` /
  `FitBandConfigUpdate`, `fit_band_config(policy)`, `GET/PUT
  /api/v1/policy/fit-bands` (thresholds, enabled bands, optional floor,
  `clear_minimum_fit_score`; validated, versioned, audited as a policy
  update); the policy page shows ranges and the config version and accepts
  the fit floor. `band_for()` is unchanged.
- **Historical reproducibility** (migration `c2f0b6d4e8a1`):
  `candidate_opportunities.fit_policy_version`, `.admission_policy_version`,
  `.gate_ruleset_version` recorded by `record_fit` / `record_admission`
  (sync and scheduler); the admission audit event carries the full gate
  report; the scheduler's "scheduled" event carries policy + ruleset
  versions. Stored matches are never rewritten; a threshold change re-bands
  at the next sync under the new version.
- **AI Gateway** (`app/ai/`): `models.py` (`AIOperation`, `AIScope` job /
  candidate, `AIStatus` OK / DISABLED / UNAVAILABLE / UNSUPPORTED /
  BUDGET_EXHAUSTED / TIMEOUT / ERROR / MALFORMED / REFUSED, `AIRequest`,
  `AIResponse.metadata()`, `TenantAISettings`, `EffectiveAIConfig`,
  `GATEWAY_VERSION`), `providers.py` (`AIProvider` protocol; `StubProvider`,
  `ScriptedProvider` for tests, `GeminiProvider` header-auth, `OllamaProvider`
  local; `build_provider`), `cache.py` (`AICache` content-addressed JSON
  files under `AI_CACHE_DIR`, key = gateway version + operation + prompt
  version + schema version + provider/model + normalized input + tenant for
  candidate scope; corrupt/stale entries are misses; `MemoryCache`),
  `budget.py` (`CallBudget` per discovery run / preparation; cache hits are
  free), `gateway.py` (`GlobalAIConfig.from_settings`, `AIGateway.effective`
  = global ∧ tenant with narrowing only, `run()` = rules → cache → budget →
  provider → validation → cache → accounting; credential-shaped prompts and
  hosted providers for candidate data refused; `UsageSink` counters +
  `ai_usage` rows; `get_gateway()` / `reset_gateway()`), `database/models.py`
  (`AIUsageRow`), `api/routes.py` (`/api/v1/ai/{config, status, usage,
  cache/clear}`; `PUT /config` writes the tenant's `policy.ai_settings`,
  versioned and audited).
- **Callers rewired**: `app/jobs/extraction/llm_provider.py` →
  `GatewayLLMProvider` (job scope, `ExtractedJobFields` schema, per-run
  budget via `LLMFallbackExtractor.start_run()`, provenance in
  `extraction_metadata["ai"]`), `GeminiProvider` / `CachingLLMProvider`
  removed; `app/preparation/ai.py` → `GatewayPolisher` (candidate scope,
  per-package budget, `metadata()`), `get_polisher(tenant_id, ai_settings)`;
  `PreparationService` records gateway metadata and truth-gate rejections in
  the preparation's audit event; the input fingerprint is unchanged.
- **Settings**: `AI_ENABLED` (false), `AI_PROVIDER`, `AI_MODEL`,
  `AI_TIMEOUT_SECONDS`, `AI_MAX_CALLS_PER_RUN`, `AI_MAX_OUTPUT_TOKENS`,
  `AI_CACHE_ENABLED`, `AI_CACHE_DIR`, `AI_CANDIDATE_DATA_PROVIDERS`,
  `AI_OLLAMA_URL`, `AI_OLLAMA_MODEL`, `AI_USAGE_PERSIST`; `LLM_PROVIDER` is
  a legacy alias for the provider choice only. Policy: `ai_settings`.
- **Tests**: `tests/ai/` (gateway: disabled / provider selection / timeout /
  error / crash / malformed / schema / cache hit-miss-stale-corrupt /
  cache-key semantics / tenant isolation / hosted-provider refusal /
  credential refusal / budget / metadata / persisted usage / singleton;
  truth boundary: zero-AI L2, faithful rewrite accepted with metadata,
  unsupported claims rejected and absent from the Evidence Graph, every
  failure mode degrades, budget per package, cache-hit second package,
  candidate scope per tenant, tenant switch, fingerprint neutrality; API +
  policy page), `tests/pipeline/test_gates.py`, `tests/pipeline/
  test_fit_bands.py`; benchmarks in `tests/ai/test_perf.py`.
- **Benchmarks (2026-09-16, AI disabled, 0 AI calls everywhere)**: gates +
  band + admission over 1,000 opportunities 0.24 s (4,150/s), 5,000 in
  1.19 s; disabled gateway path 10,000 requests in 0.19 s (53k/s); cache-hit
  path 1,000 hits in 0.64 s after 200 provider calls; preparation 1,000 L0
  101 s / L1 71 s (unchanged from Phase 4); scheduler 500 in 37 s and 5,000
  in 185 s first run / 27 s second run (unchanged from Phase 5).

## Completed in Blueprint Phase 9 (2026-09-16)

- **`extension/`** (Manifest V3, vanilla JavaScript, no framework, no build
  step, no Node toolchain): `manifest.json` (permissions `storage`,
  `activeTab`, `scripting`, `tabs`, `alarms`; host permissions for
  `http://127.0.0.1` / `http://localhost` only; optional all-sites grant for
  confirmation pages on another origin), `src/api.js` (the only network
  client: local base URL enforced, `X-API-Key` from `chrome.storage.local`,
  never in source), `src/background.js` (service worker: tab URL →
  `/extension/match` badge → `/extension/claim` → `start` with
  `BROWSER_EXTENSION` → inject page scripts → discover → `form` → fetch the
  preparation's rendered documents with `for_upload=true` and re-hash →
  fill → arm the submit interception → `/extension/gate` → result →
  handoff / confirm; per-tab state in `chrome.storage.session`; lease
  heartbeat and a one-minute settlement alarm), `src/content.js` (page
  bridge: discovery, fill, capture-phase interception of the person's or the
  extension's submit, in-page banner, post-submit observation: confirmation
  text / reference → SUBMITTED, validation error → NEEDS_REVIEW, CAPTCHA →
  HANDOFF, nothing → UNKNOWN), `src/discover.js` (**the** form discovery
  script; `app/execution/playwright/discovery.py` now loads the same file),
  `src/fill.js` (typed filling incl. `DataTransfer` file attachment),
  `src/popup.*` (per-tab status, Fill / Submit now / Pause, unanswered
  fields answered through `fields/{id}/answer` with optional answer-bank
  save, handoff confirmation "I submitted it / I did not"), `src/options.*`
  (server address, API key, submit mode manual / auto / dry-run, wait,
  optional all-sites permission), `README.md`.
- **Server side**: `BrowserExtensionExecutor`
  (`app/execution/executors/extension.py`; `can_handle` browser-form
  http(s)/file targets, in-process `prepare`/`execute` refused by design,
  conservative `verify` from reported evidence) registered in
  `default_registry()`; `ExecutionService.report_result` asks the run's
  executor to verify a SUBMITTED result that carries no verification, so a
  bare click claim is never VERIFIED; `match_for_url` (scheme / `www.` /
  query / fragment-insensitive, prefix either way, items held by another
  live worker invisible), `claim_item` (new guarded
  `QueueRepository.claim_item`), `heartbeat`, `gate` (kill switch + every
  precondition with cap refresh inside a savepoint, `submit_invoked`
  recorded on success, refused once already invoked); routes
  `/api/v1/execution/extension/{status, match, claim, heartbeat, gate}`;
  `GET /api/v1/documents/{id}/file?for_upload=true` refuses non-eligible
  artifacts; CORS middleware for `chrome-extension://` / `moz-extension://`
  origins only (`app/main.py`).
- **Tests** (`tests/execution/test_extension_api.py`,
  `test_extension_page.py`, `extension_driver.py`, `test_extension_perf.py`):
  the HTTP flow exactly as the service worker performs it (CORS allow-list,
  key required, match/claim/heartbeat/gate guards, document download hash,
  verified / unverified / unknown results, kill switch at the gate, late
  results refused, handoff + confirmation, executor contract); the page
  scripts executed verbatim under Playwright against the local fixtures with
  a stub message bridge (person-presses-submit, auto mode, validation error,
  ambiguous → UNCERTAIN and never re-gated, CAPTCHA after submit, CAPTCHA /
  login / MFA / empty pages before any fill, unknown required field waits
  for the person, popup answer saved to the bank and filled, gate refusals
  keep the click on the page, dry run blocks the submit, tampered document
  never uploaded, no password read, every source parses, manifest checks);
  50-form benchmark (200 opt-in).
- **No migration**; no new settings; no new dependency.

## Completed in Blueprint Phase 8 (2026-09-15)

- **Schema** (migration `b0d8f4a2c6e9_document_artifacts`):
  `document_artifacts` (tenant → candidate opportunity → preparation →
  artifact type → format → version; status ACTIVE / INVALIDATED,
  validation status + report, SHA-256 of the bytes, input fingerprint,
  evidence fingerprint, renderer + version, byte size, page count, relative
  path, created / invalidated timestamps and reason; unique per
  preparation/type/format/version); `execution_runs` gains
  `resume_artifact_id` and `cover_letter_artifact_id`.
- **`app/documents/`**: `DocumentModel` built verbatim from the Phase 4
  blocks (sections in preparation order, claims as bullets; cover-letter
  paragraphs greeting → intro → fit → body → closing, signature split from
  the closing block) plus the identity header from the profile and the job
  facts already in the preparation; `PdfRenderer` (fpdf2, A4, clean
  single column, clickable links, page numbers on multi-page output, fixed
  metadata, core font by default and an embedded TrueType font only when
  the content needs glyphs outside Latin-1); `DocxRenderer` (python-docx,
  same model, real hyperlinks and bullets, normalised zip timestamps);
  `validate_document` (parses, > 0 pages, no empty page, page bound →
  NEEDS_REVIEW, every expected content line present → else FAILED);
  `ArtifactStore` (identifier-validated tenant/co/prep paths, write-once
  atomic writes, verify magic / size / SHA-256 / bound); `DocumentService`
  (fingerprint over preparation id+version+fingerprint, model, options,
  renderer, version and the font used; reuse when unchanged and intact;
  invalidate-then-new-version otherwise; `ensure_for_execution`;
  `materialize` refuses INVALIDATED, FAILED and NEEDS_REVIEW for upload);
  API `/api/v1/documents/{render, ensure/{prep}, by-preparation/{prep},
  {id}, {id}/materialize, {id}/file, {id}/invalidate, {id}/regenerate}`;
  documents section with render buttons on the preparation page.
- **Execution integration**: `ExecutionService.start` renders or reuses the
  resume PDF (and the cover letter when the band enables one) before the
  attempt is taken, records the artifact ids on the run and the paths +
  hashes in the package; a document problem parks the attempt as
  NEEDS_REVIEW with the reason; `PlaywrightExecutor` uploads only the
  package's rendered file after re-hashing it, with the personal
  `EXECUTION_*_FILE` settings as a fallback; uploads are recorded in the
  run diagnostics.
- **Cap rollover fixed**: the pre-submit gate now runs the precondition
  check with `reserve=True`, so an attempt admitted in a previous
  tenant-local day/week takes a slot in the current period through the
  atomic ledger and releases the old one, committed before the click; when
  the new period is full the item waits until the period ends (attempt
  READY, nothing counted twice) instead of NEEDS_REVIEW.
- **Security**: `data/documents/` git-ignored; relative paths only in the
  API and DB; audit events carry hashes and counts, never document text;
  temp files are created next to the target and renamed atomically;
  downloads require the API key.
- Dependencies: `fpdf2`, `python-docx`, `pypdf` (pure Python, local).
- **Benchmark** (100 deterministic preparations → 100 resumes + 100 cover
  letters as PDF, core font, SQLite, run alone): 200 documents in 29 s
  (~420 per minute, ~0.14 s each including validation, hashing and the
  write), 400 DB writes (2 per document), ~2.8 KB resumes / ~2.2 KB
  letters, 11.3 MB Python peak, 0 AI calls; a second pass over the same
  preparations reused all 200 artifacts in 6 s with 0 DB writes and no new
  files. Embedding a TrueType font (only when content needs non-Latin-1
  glyphs) costs about 1–2 s per document.
- Tests: 30 new (`tests/documents/`: rendering, validation, determinism,
  storage security, service versioning / reuse / invalidation, tenant
  isolation, audit hygiene, API, 100-document benchmark, opt-in 500;
  `tests/execution/test_documents_integration.py`; Playwright: generated
  PDF received by the form, tampered file refused, cap rollover at the
  gate, exhausted cap waits).

## Completed in Blueprint Phase 7 (2026-09-14)

- **No migration.** Phase 7 is code, configuration and tests only.
- **`app/execution/playwright/`** — the first real `Executor`:
  `BrowserSession` (one local Chromium per worker, page per execution,
  explicit navigation/action timeouts, `atexit` close, relaunch after a
  browser-level crash, optional *local* persistent profile), `Pacer`
  (minimum delay between page loads), `scan_page()` (one in-page script:
  visible controls → label / type / required / options / stable selector;
  CAPTCHA, login wall, MFA and custom-widget detection; submit buttons;
  validation errors; success marker and reference extraction; no HTML
  persisted), `strategies.py` (Greenhouse, Lever, Ashby with
  `strict_controls`, generic: submit locators, success URL/text patterns,
  known field names), `PlaywrightExecutor` (`can_handle` / `prepare` /
  `execute(gate=…)` / `verify`).
- **Safety in `execute`**: fills only fields whose mapped answer is
  ANSWERED, by type (text / textarea / email / phone / select / radio /
  checkbox / multi-select / numeric / date / file); uploads only an
  existing non-empty local file (`EXECUTION_RESUME_FILE`,
  `EXECUTION_COVER_LETTER_FILE`), otherwise a required upload is the
  `ARTIFACT_FILE_REQUIRED` handoff; rescans for a late challenge; needs an
  identifiable submit control; **dry-run (default) stops here**; the
  pre-submit gate re-runs every Phase 6 precondition plus the kill switch
  (`PreconditionChecker.check(executing=True)`); then one click and a
  bounded poll: confirmation text / URL → SUBMITTED, validation error →
  NEEDS_REVIEW, CAPTCHA → HANDOFF, page gone / timeout → UNKNOWN.
- **Verification** uses only page evidence: reference on the page →
  VERIFIED (`application_id`), confirmation text → VERIFIED, confirmation-
  looking URL alone → LIKELY, otherwise UNKNOWN.
- **Handoffs** (`HandoffReason`): CAPTCHA_REQUIRED, AUTH_REQUIRED,
  MFA_REQUIRED, UNSUPPORTED_FORM (new; Ashby custom widgets),
  AMBIGUOUS_FORM, UNKNOWN_REQUIRED_FIELD, ARTIFACT_FILE_REQUIRED (new).
  Each records the page, where execution stopped and what remains; with a
  headed browser the executor can wait `PLAYWRIGHT_HANDOFF_WAIT_SECONDS`
  for the person to clear a challenge, never touching it.
- **Mapping fix**: an exact question match (prepared, then bank) now
  outranks any category match, so "Earliest start date" takes the bank's
  date instead of the notice-period prose. Profile facts gain
  `first_name` / `last_name` (a split of the stored name).
- **Service**: `ExecutionOutcome.DRY_RUN` / `ExecutionStatus.DRY_RUN`
  (attempt back to READY, item parked for review with the mapping report),
  kill-switch check before execution (item waits 10 minutes),
  `_pre_submit_gate` callback, `PLAYWRIGHT_LOCAL` in the default registry
  when Playwright is importable (browser launched lazily), `artifact_files`
  and `dry_run` in the package's `execution_config`, `target_for` hints
  `PLAYWRIGHT_LOCAL` for browser forms.
- **Local worker**: `python -m app.execution.worker --tenant default
  [--once] [--dry-run|--live] [--headed]`: claims SUBMIT items, one
  browser, one item at a time, clean shutdown on Ctrl-C.
- **Legacy v5 submit** is a compatibility wrapper: kill switch and
  approval checks stay; a dry run records `legacy_dry_run` and changes
  nothing; a live call is refused (`legacy_submit_refused`) with a pointer
  to `/api/v1/execution`. It can no longer mark anything SUBMITTED.
- **Security**: `.gitignore` covers browser profiles, execution artifacts
  and traces; diagnostics go through `sanitize_diagnostics`; screenshots are
  opt-in, local, handoff-only; no cookies/tokens/passwords are logged or
  stored; `playwright` is the `browser` extra, `tzdata` a dependency.
- **Dashboard**: the attempt page shows executor, target strategy, dry-run
  flag, steps / where it stopped, filled / skipped / found field counts,
  missing fields, the page link, and a "Resume after handoff" action.
- **Benchmark** (100 local fixture forms, headless Chromium, mixed: 75
  success across Greenhouse / Lever / Ashby, 5 validation errors, 5
  CAPTCHA, 5 login, 5 MFA, 5 ambiguous): 120 s wall (1.2 s per form, ~50
  forms/min), 1 browser launch, form discovery 6.4 s over 295 scans, 85
  submit clicks (exactly the 75 + 5 + 5 that reached the button), 75
  VERIFIED, 5 NEEDS_REVIEW, 15 BLOCKED, 5 UNCERTAIN, ~2,750 DB writes,
  7.8 MB Python peak (browser memory not measured), 0 AI calls.
- Tests: 32 new (6 discovery, 24 executor, 2 benchmark) under
  `tests/execution/test_playwright_*.py` against 13 local HTML fixtures;
  Playwright tests skip when the runtime is absent.

## Completed in Blueprint Phase 6 (2026-09-14)

- **Schema** (migration `a9c7e3f1b5d8_execution_foundation`): `execution_runs`
  (one executor invocation: idempotency key, run number, status/outcome,
  `submit_invoked`, URLs/ids/reference, verification status+method+detail,
  error class, handoff reason + detail, preconditions, sanitised
  diagnostics), `form_snapshots` + `form_fields` (structure only; per field
  the mapped answer, source, status, category, evidence keys, reason;
  never HTML, never credentials); `applications` gains `status_reason`,
  `blocked_reason`, unique `submission_key`, `execution_count`,
  `last_execution_id`, `verified_at`, `external_application_id`,
  `result_url`; `scheduler_runs.execution_enqueued`; `match_runs.tenant_id`
  (recorded by `run_matching(tenant_id=...)`); `opportunities.company_key`
  (normalised, indexed, backfilled) for O(1) company lookups.
- **Attempt state machine** (`ApplicationStatus` + `ATTEMPT_TRANSITIONS`):
  RESERVED=QUALIFIED → PREPARING → READY(_FOR_EXECUTION) → SUBMITTING
  (executing) → SUBMITTED → VERIFIED; branches BLOCKED (handoff, with
  `blocked_reason`), NEEDS_USER_INPUT, NEEDS_REVIEW, UNCERTAIN (unknown
  submit outcome), FAILED (permanent), CANCELLED, CLOSED. Ownership:
  attempt = lifecycle, queue item = scheduling/lease, preparation =
  material readiness, execution run = one invocation's result.
- **`app/execution/`**: `ExecutionPackage` (canonical input keyed by
  tenant / candidate opportunity / opportunity / preparation / attempt;
  target, artifacts, prepared answers, approved bank, profile facts,
  provenance, execution config), `target_for()` (ATS family, method,
  executor hint; discovery adapters stay read-only), `map_fields()`
  (files → artifacts, identity → profile, exact key / category → prepared
  answer → bank, typed fit for select/radio/checkbox/numeric/date,
  voluntary questions and missing facts → NEEDS_USER_INPUT, unknown types
  never filled), `PreconditionChecker` (attempt READY, preparation
  READY/current/validated/answers complete, stale by input fingerprint,
  opening open, blocklist, cool-down, duplicates, caps), `Executor`
  protocol (`can_handle / prepare / execute / verify`, `ExecutorError(before_submit)`),
  `MockExecutor` (scripted outcomes, `submit_calls` counter, no I/O),
  `ManualExecutor` (always hands off), `ExecutionService` (ready, preview,
  enqueue_ready, claim, start with the guarded READY→SUBMITTING mutex,
  execute, capture_form, report_result, verify, confirm, handoff, retry,
  cancel, answer_field, run_queue, summary), API `/api/v1/execution/*`,
  dashboard `/dashboard/execution` (+ attempt page with form answers,
  runs, confirm/retry/cancel).
- **Cap semantics at submission**: the admission reservation is consumed
  when the submission happens in the same tenant-local day/week; a new
  period refreshes it through the ledger (old slot released); no slot → the
  item waits until the period ends (not a failure); permanent failure and
  cancellation release; retries, handoffs and unknown results keep it.
- **Idempotency**: `submission_key` guarded UPDATE + `submit_invoked`
  committed before the executor acts; crash/timeout/lost lease/late result
  after that → UNKNOWN → UNCERTAIN → only `verify`/`confirm` move it on;
  `retry` is refused while an UNKNOWN run is unverified; a late result from
  a worker that lost its lease is rejected.
- **Scheduler integration**: READY attempts get SUBMIT items from
  `ExecutionService.enqueue_ready()` at the end of every scheduler run;
  scheduler decisions understand BLOCKED / NEEDS_USER_INPUT / NEEDS_REVIEW
  / VERIFIED attempts; VERIFIED counts as completed.
- **Benchmark** (SQLite, tracing on, mixed population 50% success, 5% each
  likely / retryable / permanent / CAPTCHA / auth / unknown / stale /
  blocked / cooling / already submitted): 1,000 READY attempts in ~101 s
  (~600/min), exactly 600 submit invocations (one per attempt that reached
  submit), 500 VERIFIED + 51 SUBMITTED (likely) + 50 UNCERTAIN + 150
  BLOCKED + 50 PREPARING (stale → re-prepare) + 50 CLOSED + 50 FAILED + 50
  READY (retry), ~24,200 DB writes, 4.3 MB peak traced, 0 AI calls.
- Tests: 55 new under `tests/execution/` (flow, preconditions, forms,
  identity / tenancy / concurrency, API + dashboard, 1 opt-in 5,000
  benchmark, 1 PostgreSQL-only parallel-workers test).

## Completed in Blueprint Phase 5 (2026-09-13)

- **Schema** (migration `f7a5c1e9d3b6_scheduler_runs_and_caps`):
  `scheduler_runs` (per-tenant run record: status, trigger, policy version +
  snapshot, window, heartbeat, counts considered / admissible / admitted /
  enqueued / requeued / already_queued / already_completed /
  ready_for_execution / needs_review / needs_user_input / released /
  deferred / blocked, `blocked_by_reason`, samples, preparation report,
  errors), `application_cap_ledger` (tenant × DAY|WEEK × period key, reserved
  / released), `applications` gains `candidate_opportunity_id`,
  `attempt_number`, `lane`, `tailoring_level`, `cap_day`, `cap_week`,
  `reserved_at`, `released_at`; `candidate_opportunities` gains the
  scheduler's last decision (`scheduler_code` / `_reason` / `_run_id` /
  `_decided_at`); `application_policies` gains `minimum_fit_score` and
  `timezone`.
- **One policy engine.** `evaluate_admission` now returns an
  `AdmissionReason` code and takes the fit score (optional
  `minimum_fit_score` floor); the blocklist compares `company_key()`
  (case, punctuation, accents and legal suffixes folded). The scheduler's
  `decisions.decide()` layers the stateful checks on top in a fixed order:
  skipped → closed → existing attempt (ALREADY_SUBMITTED / DUPLICATE_OPPORTUNITY
  / ALREADY_IN_PROGRESS / NEEDS_USER_INPUT / NEEDS_REVIEW) → static policy →
  cancelled/failed PREPARE item → preparation waiting on a human →
  DUPLICATE_APPLICATION → COOLDOWN_ACTIVE → paused caps → ADMITTED. Caps and
  the run window are applied after the verdict, in order.
- **Caps count attempts.** Admission reserves an `applications` row and one
  slot in the tenant-local day and ISO week (policy `timezone`) through a
  conditional `UPDATE` on the ledger, so concurrent workers cannot exceed a
  cap. Discovery, matching, preparation and retries never count; cancelled,
  permanently failed, closed or newly blocked attempts release their slot.
  `cap = 0` pauses admission.
- **Cool-down** is per normalised company, starts at submission (or at
  admission for an in-flight attempt, so one opening per company per
  window), ends inclusively at `start + cooldown_days` (T−ε blocked, T
  allowed), `0` disables. Reposts after a submission follow
  `duplicate_policy` (BLOCK → DUPLICATE_OPPORTUNITY;
  ALLOW_REPOST_AFTER_COOLDOWN → admissible once the cool-down passes).
- **`app/scheduler/`**: `caps.py` (`period_keys`, `CapLedger`),
  `attempts.py` (`AttemptRepository`: reserve / mark_ready / mark_preparing /
  release, cool-down + duplicate indexes), `decisions.py` (pure `decide`,
  `ordering_key` = priority desc, deadline asc, first_seen desc, id),
  `service.py` (`SchedulerService.preview` read-only, `.run` with heartbeat
  lease, savepoint per admission, commit every 100, optional
  `run_prepare_queue` orchestration, reconcile before and after,
  `.capacity`, `.history`, `.ready_for_execution`), API
  `/api/v1/scheduler/{preview,run,status,capacity,runs,runs/{id},queue}`,
  dashboard `/dashboard/scheduler` (+ run detail).
- **Tenancy**: `POST /api/v3/matches/recalculate` scores against the
  requesting tenant's Career Brain and syncs into that tenant explicitly; the
  v5 application routes pass the tenant; `ApplicationEngine.get_or_create`
  resolves `(tenant, opportunity)` first and derives the opportunity from a
  job id, so two source rows never yield two attempts.
- **Benchmark** (shared SQLite test DB, tracing on, mixed population of
  HIGH/MEDIUM/LOW/INELIGIBLE, blocked, cooling, queued, submitted, review,
  user-input, duplicate source rows): 501 candidate opportunities scheduled
  in ~7 s (~4,300/min; 300 admitted + enqueued, every refusal counted by
  reason, 25 duplicate source rows collapsed, ~4,400 DB writes over two
  runs, ~6 MB peak, 0 AI calls); second run 0.5 s and idempotent. 5,001
  candidate opportunities (opt-in, `CAREEROS_PERF=1`): first run 87 s
  (~3,400/min; 3,000 admitted + enqueued, 250 blocked per reason, 250
  already queued, 251 already submitted, 250 duplicate source rows
  collapsed), second run 5.7 s, ~43,800 DB writes over both runs, ~61 MB
  peak traced, 0 AI calls.
- Tests: 84 new under `tests/scheduler/` (policy matrix of 28 cases, caps,
  cool-down/blocklist/duplicates, ordering/idempotency/tenancy,
  orchestration, application identity, API + dashboard, 1 opt-in 5,000
  benchmark, 1 PostgreSQL-only ledger concurrency test).

## Completed in Blueprint Phase 4 (2026-09-12)

- **Schema** (migration `e6f4b0d8c2a5_application_preparation`):
  `application_preparations` (tenant → candidate opportunity → versions;
  level, lane, cover-letter mode, positioning variant + version, status,
  validation status/report, `input_fingerprint` + `inputs`, evidence keys,
  AI provider/model/calls), `preparation_artifacts` (RESUME / COVER_LETTER
  as ordered blocks with per-block evidence keys plus rendered text),
  `preparation_answers` (question, normalised key, category, source,
  status, evidence keys, answer-bank link). `application_policies` gains
  `cover_letter_by_band`; `applications` gains `preparation_id`.
- **`app/preparation/`**: `EvidenceSnapshot` (one load per tenant; only
  CONFIRMED + application-safe nodes may back claims; version fingerprint),
  `select_evidence` (matched requirements first, then the positioning
  variant, then remaining confirmed evidence, capped per level),
  `select_variant` (deterministic, never invented), `compose_resume` /
  `compose_cover_letter` (string-built from cited nodes; FRAMING vs CLAIM
  blocks), `PreparationValidator` (feeds every CLAIM through
  `TruthValidator.validate_claim` and adds unresolved/removed/unsafe
  evidence, unsupported number/date/skill checks; job facts and
  candidate-authored framing are allowed context, never evidence),
  `AnswerResolver` (exact key → category → profile fact → evidence template
  → NEEDS_USER_INPUT / NEEDS_REVIEW), `PreparationService` (batch loads,
  fingerprint idempotency, versions with SUPERSEDED, review actions,
  candidate-answer capture with optional save-to-bank, audit events),
  `queue_worker` (PREPARE → SUCCEEDED/READY_FOR_EXECUTION | BLOCKED for
  user input | NEEDS_REVIEW | retry on failure), optional `TextPolisher`
  (stub default; rewrites re-validated, rejected ones keep the original).
- **Tailoring levels** are resource decisions only: L0/L1 deterministic and
  zero-AI, L2 optional AI; eligibility is never consulted or changed.
- **API** `/api/v1/preparations` (prepare, batch, run-queue, by-opportunity,
  detail, evidence trace, approve, reject, invalidate, answer) and
  **dashboard** `/dashboard/preparations` (+ detail with "why is this
  here" evidence column, answer forms, regenerate).
- `ApplicationEngine.get_or_create` now records `tenant_id`,
  `opportunity_id` and `preparation_id` when provided (Phase 2 gap A).
- **Performance**: the taxonomy scan gained a literal pre-filter and the
  snapshot caches per-node numbers/skills, so validation never rescans
  evidence. Benchmark (shared SQLite test DB, memory tracing on): 1,000
  opportunities in ~42 s per level (~1,400 packages/min, 7 DB writes each,
  ~25 MB peak traced), 0 AI calls at L0 and L1. 200 packages profiled
  without tracing: 3.6 s.
- Tests: 46 new under `tests/preparation/` (1 opt-in 5,000 benchmark).

## Completed in Blueprint Phase 3 (2026-09-11)

- **Ingestion order per job**: validate → raw hash → *unchanged fast path*
  (touch `last_seen`, refresh freshness, resolve opportunity; no extraction)
  → normalise → optional LLM fallback → dedup → persist → version only if
  content changed → `OpportunityRepository.resolve_opportunity` (the single
  creation path; every persisted job has exactly one link).
- **Schema** (migration `d5e3a9c7b1f4_discovery_volume`): `jobs.freshness`
  (indexed), per-observation `content_hash` / `fetched_at` / `parse_status`
  on `job_source_references`, twelve volume/AI/network counters plus
  `failure_kind`, `company_name`, `resumed_from_run_id`, `checkpoint` on
  `discovery_runs`, and the shared `source_health` table.
- **Adapters** (`app/jobs/sources/base.py`): `SourceError.kind`
  (not_found / forbidden / rate_limited / server_error / network /
  invalid_response / circuit_open), 401/403 never retried, jittered backoff,
  per-source-type circuit breaker, per-run `RequestStats`.
- **Repost handling**: a closed row seen again is reopened and counted as a
  repost; a distinct row for a closed opening links to the same opportunity
  with `repost_count`/`reposted_at`; the sweep closes opportunities whose
  jobs are all closed. Never a brand-new opportunity for a known opening.
- **Freshness** (`app/jobs/freshness.py`): FRESH ≤2d, RECENT ≤7d, AGING ≤30d,
  STALE, UNKNOWN (no date is never invented; closed = STALE).
- **Source health** (`app/jobs/pipeline/source_health.py`): success/partial/
  failure/rate-limited counts, consecutive failures, last error and kind,
  fetched/parsed/rejected totals, freshest posting, average duration,
  `next_poll_at` with doubling back-off (max 24h, never auto-disabled).
- **Resumability**: `resume_from_run_id` skips postings the interrupted run
  already observed; `checkpoint` on the run row; `POST /discovery/runs/{id}/resume`.
- **Extension capture contract** `CapturedJob` (`app/jobs/sources/capture.py`)
  and `POST /api/v2/discovery/capture`: schema.org JobPosting JSON-LD first,
  DOM fields as fallback, same pipeline, no sweep.
- **Scheduling**: `POST /api/v2/discovery/run-due` polls only due boards;
  `run-batch` registers boards; the workflow now has a 2-hourly poll cron
  beside the twice-daily broad sweep.
- **Candidate projection** after each run: DISCOVERED
  `candidate_opportunities` rows for `DISCOVERY_PROJECT_TENANTS` (inserts
  only; eligibility/fit/priority stay in the match sync).
- **API/dashboard**: run responses carry all counters and jobs/minute;
  `/api/v2/discovery/sources`, `/circuits`; `/dashboard/sources`; richer
  `/dashboard/runs`; `freshness` filter on `/api/v2/jobs`.
- **AI**: `LLMFallbackExtractor.calls` / `cache_hits`; the stub provider
  never runs; per-run `ai_calls` and `ai_cache_hits` are recorded.
- Tests: 28 new under `tests/discovery/` including a seeded synthetic
  volume fixture (500 always; 5,000 with `CAREEROS_PERF=1`).

## Completed in Blueprint Phase 2 (2026-09-11)

- **Schema** (migration `c4d2f8a1e6b3_opportunities_policy_queue`). Shared:
  `opportunities` (identity key = company | normalised title | location
  bucket, exact match only), `opportunity_jobs` (source rows → opportunity,
  repost flag). Tenant: `candidate_opportunities` (unique per tenant +
  opportunity; state, eligibility, fit, band, priority, admission),
  `eligibility_decisions`, `priority_scores`, `application_policies`,
  `application_queue` (unique `(tenant, opportunity, action)` and
  `idempotency_key`). `applications` gains nullable `tenant_id` +
  `opportunity_id`, unique together.
- **Three separate concepts** in `app/pipeline/models.py`: eligibility
  (ELIGIBLE / LIKELY / UNCERTAIN / INELIGIBLE / REVIEW — UNCERTAIN ranks
  above INELIGIBLE and is admitted by default), fit band (candidate
  thresholds over Phase 3's score), priority (0–100, order only).
- **Priority scorer** (`app/pipeline/priority.py`): deterministic weighted
  sum of fit, freshness, deadline urgency, source reliability, execution
  ease and a neutral learned prior; weights per tenant, versioned.
- **Policy** (`app/pipeline/policy.py`, `PolicyRepository`): enabled bands,
  thresholds, caps, tailoring and lane per band, cool-down, blocklist,
  minimum eligibility, duplicate policy, priority weights. `evaluate_admission`
  never reads priority.
- **Repositories**: `OpportunityRepository` (shared identity resolution,
  candidate state machine with guarded transitions, decisions, fit link,
  priority, admission, cool-down helper), `QueueRepository` (idempotent
  enqueue, claim with `FOR UPDATE SKIP LOCKED` on PostgreSQL and a guarded
  update on SQLite, lease, start/succeed/fail-with-backoff/block/
  needs-review/cancel/requeue/reclaim). All writes audited in `audit_events`.
- **Sync** (`app/pipeline/sync.py`): projects a match run into candidate
  opportunities (decision from the match's eligibility, fit band, priority,
  admission); wired after `/api/v3/matches/recalculate`. Discovery resolves
  the shared opportunity for every persisted job inside the same savepoint.
- **API**: `/api/v1/opportunities` (list/filter/summary/detail/eligibility/
  priority/state/enqueue/sync), `/api/v1/policy` (get/put),
  `/api/v1/queue` (list/summary/claim/worker ops/cancel/requeue/reclaim).
- **Dashboard**: `/dashboard/opportunities`, `/dashboard/queue`,
  `/dashboard/policy` (editable form).
- `ordered_db_now()` in `app/core/timeutils.py`: strictly increasing audit
  timestamps within a process (Windows clock ticks made same-tick events
  sort arbitrarily).
- Tests: 47 new under `tests/pipeline/`; suite total 410.

## Completed in Blueprint Phase 1 (2026-09-11)

- **Schema** (migration `a3c1e5f7b9d2_create_career_brain_tables`): `tenants`
  (default tenant seeded), `candidate_profiles`, `evidence_nodes`,
  `evidence_relationships`, `positioning_variants`,
  `positioning_variant_evidence`, `answer_bank_entries`, `audit_events`.
  Every candidate-side row carries `tenant_id`; node identity for downstream
  references is the tenant-scoped `key` (unique per tenant).
- **Domain** (`app/career/models.py`): `EvidenceKind`, `EvidenceGrade`
  (CONFIRMED / APPROXIMATE / UNVERIFIED / NEEDS_REVIEW / REMOVED, derived from
  the existing `VerificationStatus`, never a second stored field),
  `EvidenceSourceType`, `RelationType`, node/variant/answer/audit models.
- **Repository** (`app/career/repository.py`): tenant-bound reads and writes;
  every mutation bumps `version` and writes a before/after audit event; truth
  rules enforced (INFERRED/LLM_EXTRACTED can never be created VERIFIED, only
  VERIFIED can be application-safe); positioning variants and answers may
  only reference existing active evidence.
- **Importer** (`app/career/importer.py`, `python -m app.career.importer`):
  idempotent, hash-keyed upsert of `career_seed.json`; derives METRIC,
  RESPONSIBILITY, EDUCATION and LINK nodes and DEMONSTRATES/HAS_METRIC/
  HAS_RESPONSIBILITY/ABOUT relationships; reports created/updated/skipped.
- **Read model** (`app/career/read_model.py`): rebuilds Profile, Skill,
  Project, Experience, Achievement, CareerFact, Preference from the graph.
- **`CareerBrainService`** reads from the database by default and bootstraps
  a tenant from the seed on first use; `data_path=` keeps legacy JSON mode.
- **API** `/api/v1/career/*`: profile, preferences, evidence CRUD with soft
  removal/restore/history, relationships, positioning, answers (with
  deterministic `lookup`), import, audit. Writes require `X-API-Key`.
- **Dashboard** `/dashboard/profile/`: evidence by kind with grade and
  provenance, status change, edit, remove/restore, profile details,
  positioning variants, answer bank, recent changes. htmx `hx-boost` added.
- Tests: 67 new under `tests/career/`; suite total 363 (296 after Phase 0).

## Completed in Blueprint Phase 0 (2026-09-11)

- `docs/BLUEPRINT.md` — the revised blueprint checked into the repo as the
  source of truth, with the phase plan and target data model.
- `app/core/errors.py` — one structured error hierarchy (`CareerOSError` with
  stable `code`, `http_status`, `retryable`) and a FastAPI handler that returns
  `detail` + `error{code,message,details}` + `request_id`.
- `app/core/logging.py` — root logging with a `RedactingFilter` that scrubs
  API keys, bearer tokens, cookies, session ids and known key prefixes from
  every log record. Installed by `app/main.py`.
- `app/core/ids.py` — shared `new_id()`.
- `app/config.py` — `DEPLOYMENT_MODE` (solo|hosted), `DEFAULT_TENANT_ID`, and
  Postgres pool settings; `app/database.py` applies pre-ping/recycle/pool size
  on non-SQLite engines (free-tier Postgres drops idle connections).
- `docker-compose.yml` — local Postgres 16 with pgvector; CI's Postgres job
  now uses the same image.
- Lint is green (`ruff check app tests`): a real `F821` (undefined name in
  `app/models/claim.py`), unused variables/imports and import order fixed;
  ruff targets py311, `UP` rules dropped and `E501` ignored to avoid
  mechanical churn on the existing codebase.
- `pyproject.toml`: version 0.5.0, `requires-python >= 3.11`.
- Tests: 296 passing (278 before Phase 0), including new error-contract and
  redaction tests.

## Real forms completed end to end: searchable dropdowns, yes/no buttons, new fact categories (2026-09-22)

Why: after eight days of autopilot the real `careeros.db` held 0 submissions. Every Greenhouse
job-boards form asks Country and Location (City) in react-select comboboxes and the executor
refused them ("a searchable dropdown: CareerOS cannot choose an option"); Ashby asks yes / no
questions with two buttons; "current company", "how did you hear about us", "have you worked
here before", conflict-of-interest and acknowledgement boxes all fell to "cannot be grounded".
Also every autopilot re-match created new match ids, so READY packages went STALE and bounced
back to PREPARING, and PREPARE items parked on "needs_user_input" never re-ran after the person
approved the answers in Career Brain.

- `extension/src/discover.js`: `role=combobox` inputs are `field_type: combobox` (options only
  when a list is already rendered; `current_value` from react-select's single-value element);
  yes / no button questions are `field_type: yesno` with the buttons' texts as options; visible
  "Apply" links / buttons are reported as `apply_links`.
- `app/execution/models.py`: `FieldType.COMBOBOX`, `FieldType.YESNO`.
- `app/execution/forms.py`: `choose_option` (exact → yes/no polarity → "City, State, Country"
  parts → unique containment → degree level), `search_terms` (what to type: the answer, the city,
  the bare yes/no, the degree level), `names_other_country`. A combobox answer is carried as text
  and matched on the page; a yes / no-shaped question never receives a non-yes/no fact. Profile
  facts now include `country`/`city` (split from the recorded location), education facts
  (`college`, `degree`, `cgpa`, `graduation_year`, `branch`) answering education sub-questions by
  label, and `employers` (EXPERIENCE evidence). Guards: authorisation / sponsorship answers are
  not reused for a question naming another country; "worked here before?" is never answered
  from the bank for a company on the candidate's record; consent is ticked only on required
  choice controls (optional opt-ins skipped, typed acknowledgements are the person's); EDUCATION,
  CONFLICT_OF_INTEREST and topic-specific EXPERIENCE_YEARS are exact-question only.
- `app/preparation/questions.py`: categories `CURRENT_EMPLOYER`, `CURRENT_TITLE`,
  `REFERRAL_SOURCE`, `PRIOR_EMPLOYMENT`, `CONFLICT_OF_INTEREST`, `CONSENT` (all facts → NEEDS
  USER INPUT until the bank holds them); LOCATION and EDUCATION rules widened.
- `app/execution/playwright/executor.py`: `_pick_combobox` (type, wait for `[role=option]`,
  `choose_option`, click, confirm the control shows the choice, otherwise leave it empty),
  `_press_yesno` (press the matching button, confirm `aria-pressed`), `fill_notes` →
  `diagnostics.unfilled_notes`, `_follow_apply_link` (a listing page's own Apply link is followed
  once; navigation only), and a page with no fields is always AMBIGUOUS_FORM (a search button is
  not a form).
- `app/preparation/service.py`: `match_fingerprint` (evidence references + requirement statuses)
  replaces `match_id` as the staleness input; legacy packages tolerate the missing key.
- `app/scheduler/service.py`: blocked PREPARE items are re-queued when the approved answer bank
  changed since they were parked (`reprepare_requeued` counter, migration `f2c6a4b8d1e7`); a
  PREPARE item blocked by policy on a no-longer-admitted opportunity releases the attempt.
- Desktop: the guided profile step lists current employer, referral source and standard
  acknowledgements among the standard answers.
- Tests: `tests/execution/test_playwright_searchable_dropdowns.py` (fixture
  `greenhouse_select.html`: dry run sets every dropdown, live run submits with the chosen options,
  an unknown place stops before submit), `test_playwright_apply_link.py`, rewritten
  `test_playwright_unfillable_required.py`, `test_forms_real_forms_2026_09_22.py`,
  `tests/preparation/test_question_categories_2026_09_22.py`, `test_stale_match_id.py`,
  `tests/scheduler/test_reprepare_after_answers.py`.

## Known Issues / open gaps

- **Comboboxes / typeaheads are discovered as text inputs** (Greenhouse
  "Country" / "Location (City)", Lever "Current location"): typing without
  selecting an option does not satisfy the form. They surface as
  `NEEDS_USER_INPUT` today (no profile fact), so nothing wrong is typed, but
  answering them needs option selection (Phase 13, P2).
- **An invisible challenge widget is not treated as a wall** (Greenhouse
  hosted boards and embeds carry a reCAPTCHA badge, Lever an invisible-mode
  hCaptcha): discovery reports it
  (`captcha_visible` false, diagnostics `captcha_invisible`), the run
  proceeds, and a challenge the provider shows *after* the click hands off
  after the click. Nothing is solved by the system; a visible widget or
  challenge text still hands off before anything is typed (Phase 13).
- **Bot protection is intermittent on real boards**: one Ashby page showed a
  challenge on a later visit that an earlier visit had not; keep
  `PLAYWRIGHT_MIN_DELAY_SECONDS` generous.
- **The extension's content script runs in the top frame only**; forms
  embedded in an ATS iframe (Greenhouse on employer sites) are invisible to
  it until `all_frames` injection is added (Phase 13, P2).
- **True multi-process concurrency is proven on PostgreSQL in CI only.**
  Locally the thread races run on SQLite (single writer, WAL) with retry
  on `database is locked`; they prove the guarded statements, not
  PostgreSQL row locking, which the two PostgreSQL-only tests cover in CI.
- **Recovery is lease-based.** A worker that is alive but silent past
  `EXECUTION_LEASE_SECONDS` without heartbeating is treated as lost; its
  gate is refused and its item is reclaimable. Long manual sessions must
  keep heartbeating (the extension does).
- **Learning statistics are associational.** The engine reports what was
  observed in this candidate's own history; it cannot say what would have
  happened under another lane, level, executor or source (difficult sites
  may be routed to one executor, high-fit roles to one lane).
- **The learned ordering signal uses four dimensions** (source, company,
  normalized title, fit band) because lane, level and variant are chosen
  after admission; preparation and execution statistics are informational.
- **No time decay beyond the rolling window** (`window_days`); exponential
  decay is not implemented.
- **No mailbox connector exists** (by design): emails reach the Signal
  Inbox only when supplied (`POST /api/v1/signals/email`, a paste, a future
  local connector). Nothing reads a mailbox.
- **Attribution by company/title reads the stored excerpt** on reprocess
  (the first pass reads the full text); references beyond
  `SIGNAL_EXCERPT_CHARS` are only seen once.
- **Withdrawal / closure outcomes never touch the attempt lifecycle**
  (they may release a cap slot); a person cancels or closes the attempt.
- **The AI classifier is one call per ambiguous signal, budgeted per
  service instance** (`AI_MAX_CALLS_PER_RUN`); it never runs for confident
  rule matches.
- **Tenant resolution is a stub.** `app/api/deps.py:get_tenant_id` returns
  `DEFAULT_TENANT_ID` for every request (single-user product, one API key).
  Data isolation is structural in the repository; per-user authentication
  is Blueprint Phase 12.
- **Seed records removed from `career_seed.json` are not removed from the
  graph** on re-import (safer default); remove them via the API/dashboard.
- **Rendered documents are plain by design.** One layout (single column,
  A4), a handful of settings; no theme engine. A resume above
  `DOCUMENTS_MAX_PAGES_RESUME` pages is NEEDS_REVIEW, not uploaded.
- **Non-Latin scripts need a font that covers them.** The bundled fallback
  is the system's Arial/DejaVu; a script the font lacks fails validation
  (content line missing) rather than rendering boxes. Set
  `DOCUMENTS_FONT_PATH` to a suitable TrueType font.
- **DOCX page counts are not measured** (python-docx has no layout engine);
  DOCX validation covers structure and content, the page bound applies to
  PDF.
- **Cover-letter date lines are opt-in** (`DOCUMENTS_LETTER_DATE`), so a
  render never carries an invented date and stays deterministic.
- **Ashby is supported only when its form uses standard controls.** Its
  React custom widgets (comboboxes, drop zones) hand off as
  `UNSUPPORTED_FORM`; the same applies to any site whose form is not made of
  `input` / `select` / `textarea` elements.
- **Discovery is heuristic.** Labels come from `<label>`, ARIA, wrappers and
  nearby headings; an unusual layout can yield a wrong label, which ends as
  NEEDS_REVIEW / SKIPPED, never a wrong fill of a high-risk fact (those
  require an exact or category match to a stored answer).
- **No live employer smoke test exists**, on purpose: there is no public
  application form that is safe to submit against. Real-site behaviour is
  validated by running the worker in dry-run mode first.
- **The legacy v5 submit route is a compatibility wrapper** that cannot
  submit; new callers use `/api/v1/execution`.
- **Lowering a cap does not pull back admitted attempts.** A slot reserved
  under the old cap is consumed as long as the submission happens in the
  same period; only a period rollover re-checks against the new cap.
- **Answer-bank / profile facts are cached per `ExecutionService`
  instance** (refreshed at each `run_queue` and after `answer_field`); a
  long-lived service should be recreated after Career Brain edits.
- **Form answers are mapped from prepared material only.** A form question
  that neither the preparation nor the approved bank covers is a question
  for the candidate; there is no generation at execution time by design.
- **A `SchedulerRunRow` left RUNNING by a crash is superseded** by the next
  run once its heartbeat is older than `RUN_LEASE_SECONDS` (600 s), or
  immediately with `force=true`.
- **Application questions default to a standard set.** Real form questions
  arrive with the executors (Phases 8/9) and are passed to
  `PreparationService.prepare(questions=...)`; an unknown question goes to
  review rather than being drafted without evidence.
- **Cool-down is company-wide.** One admitted or submitted opening per
  normalised company per `cooldown_days`; a per-team or per-role-family
  exemption is not modelled.
- **Only three structured ATS adapters plus capture exist.** ATS
  auto-detection from company URLs, aggregator/RSS adapters and company-list
  growth are still open (blueprint §5); `JobSourceType.AGGREGATOR` is reserved.
- **The circuit breaker and rate limiter are per process.** Correct for the
  single-process free tier; a shared limiter is deliberately not built.
- **Real-provider LLM fallback runs inline** in the ingestion loop when a
  provider is configured (stub by default, so no calls). Moving enrichment to
  the batch lane behind the AI gateway is Phase 7.
- **`GreenhousePlaywrightAdapter` launches a headless server-side browser.**
  The blueprint relocates Playwright to a local runner (Phase 8); the adapter
  interface is kept, the live path remains disabled until then.
- **`CachingLLMProvider` is an on-disk cache.** It is superseded by the
  DB-backed AI gateway in Phase 7; `LLMProvider` stays as the provider seam.
- **No PostgreSQL deployment exists yet.** `docker-compose.yml` and CI cover
  it; SQLite is what runs locally by default.
- **Semantic skill matching is unproven and off by default.**
- `app/intelligence/services/requirement_interpreter.py` is legacy and not
  wired into the orchestrator.
- Contact info, GitHub, LinkedIn, portfolio URLs in the Career Brain are
  `NEEDS_REVIEW` (user input pending).

## Architecture Decisions

1. **Structured ATS APIs first; Firecrawl selective, never for submission.**
2. **Deterministic first, LLM second, AI never mandatory.**
3. **Exact-equality dedup, no fuzzy merge; opportunity identity layered on top.**
4. **Domain (Pydantic) / persistence (SQLAlchemy) separation.**
5. **Alembic owns the schema.**
6. **No Redis/Kafka/Kubernetes/cloud browsers; Postgres is the queue; batch on
   GitHub Actions; execution in the candidate's browser or local runner.**
7. **Curated local skill taxonomy behind a `TaxonomyProvider` seam.**
8. **One error hierarchy, one redacting logger** (Phase 0): errors carry a
   stable code and retryability; secrets cannot reach a log line by accident.
9. **Volume is the objective.** Prioritisation orders; it never excludes an
   eligible job in an enabled band. Learning never lowers volume.

## Context Files

| File | Purpose |
|---|---|
| `docs/BLUEPRINT.md` | Revised blueprint — source of truth for implementation |
| `docs/IMPLEMENTATION_CONTEXT.md` | Module map, data flow, invariants, extension points |
| `docs/PROJECT_STATE.md` | Operational project memory (this file) |
| `docs/ARCHITECTURE.md` | System architecture as built |
| `docs/DEVELOPMENT.md` | Developer setup guide |
| `docs/OPERATIONS.md` | Invariants, recovery, health, backup, retention, indexes, SQLite/PostgreSQL parity, security summary (Phase 12), real-world validation (Phase 13) |
| `docs/validation/phase13/REPORT.md` (+ `*.json`) | Phase 13 real-world compatibility matrix and raw observations; `tools/validation/phase13_validate.py` regenerates them |
| `docs/career/*.md`, `docs/projects/*.md` | Career Context Pack |
| `data/career_seed.json` | Career Brain seed data |

## Last Updated

September 22, 2026 (real-form completion session; earlier: September 12, 2026, Phase 13 validation session; Phase 12 dated 2026-09-19 in its own section)
