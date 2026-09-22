# CareerOS Operations (Blueprint Phase 12)

Local-first operations for one candidate's application machine: what must
never happen, how the system recovers, what to back up, what may be purged,
and how to see whether it is healthy. Nothing here needs a cloud service.

## 1. Invariants (enforced in the database where possible)

| Invariant | Enforcement | Test |
|---|---|---|
| One attempt per tenant and real-world opening | `applications` UNIQUE `(tenant_id, opportunity_id)`; `candidate_opportunities` UNIQUE `(tenant_id, opportunity_id)` | `tests/hardening/test_concurrency.py::test_two_schedulers_admitting_the_same_opportunity_reserve_one_attempt` |
| One submit click per attempt | `applications.submission_key` UNIQUE + guarded `UPDATE … WHERE status='READY' AND submission_key IS NULL`; `execution_runs.submit_invoked` committed before the click; `execution_runs.idempotency_key` UNIQUE | `test_execution_safety.py`, `tests/execution/test_identity_tenancy_concurrency.py` |
| One queue item per tenant, opening and action | `application_queue` UNIQUE `(tenant_id, opportunity_id, action)` and `idempotency_key`; claims are guarded `UPDATE … WHERE state = <seen> AND claimed_by = <seen>` (`FOR UPDATE SKIP LOCKED` on PostgreSQL) | `test_concurrency.py::test_parallel_workers_never_claim_the_same_item_twice` |
| Exactly `cap` reservations per tenant-local period | `application_cap_ledger` UNIQUE `(tenant, kind, key)` + conditional `UPDATE … WHERE reserved - released < cap` | `test_concurrency.py::test_parallel_reservations_never_exceed_the_cap`, `tests/scheduler/test_caps.py` |
| One logical signal per tenant and observation identity | `signals` UNIQUE `(tenant_id, dedupe_key)`; repeats become `signal_observations` (atomic counter) | `test_concurrency.py::test_parallel_deliveries_of_one_email_make_one_signal` |
| One outcome event per signal, application, outcome and origin | `outcome_events` UNIQUE `(tenant_id, dedupe_key)`; history is superseded / retracted, never deleted | `tests/signals/test_outcomes.py`, `test_concurrency.py::test_concurrent_confirmations_record_one_human_event` |
| One derived outcome per application | `application_outcomes` UNIQUE `(tenant_id, application_id)` | `tests/signals` |
| One artifact per preparation, type, format and version; write-once files | `document_artifacts` UNIQUE `(preparation_id, artifact_type, format, version)`; `ArtifactStore.write_immutable` | `tests/documents/test_service_storage.py`, `test_security.py::test_document_hash_mismatch_is_detected` |
| Source job identity and opportunity identity | `jobs.canonical_key` UNIQUE, `opportunities.identity_key` UNIQUE, `opportunity_jobs.job_id` UNIQUE | `tests/test_dedup_collisions.py`, `tests/pipeline/test_identity.py` |
| Learning snapshots are immutable and versioned | new row per snapshot; never updated | `tests/learning/test_learning.py`, `test_concurrency.py::test_concurrent_snapshots_are_separate_and_immutable` |
| Every candidate-side row carries `tenant_id` and every query filters on it | repositories take the tenant explicitly; the API resolves it server-side | `test_security.py::test_foreign_ids_never_expose_another_tenants_objects` |

Constraint presence is asserted by `tests/hardening/test_migrations_chain.py`.

## 2. Execution safety rules

1. **Before submit** a failure is retryable: the run ends `FAILED_RETRYABLE`,
   the attempt returns to `READY`, the queue item waits with backoff.
2. **After `submit_invoked`** nothing is ever retried automatically: the run
   ends `UNKNOWN`, the attempt is `UNCERTAIN`, the item is `NEEDS_REVIEW`.
   Only `POST /runs/{id}/verify`, `/confirm`, or a STRONG confirmation
   signal through the inbox settles it.
3. The final gate (`/extension/gate` or the in-process pre-submit gate)
   re-runs every precondition: kill switch, cap (reserve/refresh), blocklist,
   duplicate, closed opening, stale preparation, artifact hash. An **expired
   lease** is refused at the gate (`LEASE_EXPIRED`); a heartbeat renews it.
4. Late results from a worker that lost its item are refused (`409`); the
   winner's `start` settles the loser's run (before submit → retryable, after
   → UNCERTAIN). Playwright and the extension therefore never both click.

## 3. Startup and recovery

On boot (`app.main` lifespan) and at the start of every `run_queue`:

* `reconcile_stale_runs` marks discovery runs left `running` past
  `STALE_RUN_TIMEOUT_MINUTES` as `interrupted`.
* `recover_execution` (Phase 12) settles every `execution_runs` row left
  `RUNNING` whose queue lease lapsed: `submit_invoked` → UNKNOWN / UNCERTAIN /
  NEEDS_REVIEW (+ a signal); otherwise → FAILED_RETRYABLE / READY / PENDING.
  Runs with a live lease are left to their worker.
* Queue leases lapse on their own: `claim` reclaims expired CLAIMED /
  PROCESSING items; `POST /api/v1/queue/reclaim-expired` does it eagerly.
* A scheduler run whose heartbeat is older than 600 s is superseded by the
  next run.
* Preparation and document generation are idempotent per input fingerprint;
  an interrupted render leaves no file (temp file + atomic rename) and is
  simply rendered again.
* Signals are idempotent per reference / content hash; re-delivering after a
  crash adds an observation, never a second signal or outcome.

`tests/hardening/test_recovery.py` covers each of these.

## 4. Health and diagnostics

* `GET /health` — liveness + schema readiness (no credentials).
* `GET /api/v1/ops/diagnostics` and `/dashboard/ops` — per tenant: queue depth
  by state and action, stale leases, attempts by status (blocked / review /
  input / uncertain / failed), RUNNING and UNKNOWN runs, 24-hour execution
  throughput, signals awaiting review, discovery runs and failing sources,
  AI gateway counters and failures, cap utilisation for the current day and
  week, kill switches, learning snapshot age, and a list of warnings. Counts
  only: no candidate data, prompts, excerpts or secrets.

Every audit event carries tenant, entity type, entity id, action, actor,
before/after and a summary; execution events carry run id, executor kind,
worker id, error class and reason. Logs pass through `RedactingFilter`
(keys, bearer tokens, cookies, JWT-shaped strings) and never contain
document text, prompts, email bodies or page content.

## 5. Backup and recovery (local-first)

Back up:

| What | Where | Notes |
|---|---|---|
| Database | `careeros.db` (SQLite; also `-wal` / `-shm` if present) or a `pg_dump` of the PostgreSQL database | The whole state: jobs, opportunities, policy, attempts, runs, signals, outcomes, audit, learning. Stop the app/worker or use `sqlite3 careeros.db ".backup 'careeros-YYYY-MM-DD.db'"` for a consistent copy under WAL. |
| Generated documents | `DOCUMENTS_ROOT` (`data/documents/`) | Immutable versioned PDF/DOCX files; their SHA-256 is in `document_artifacts`, so a restored file is verified before upload. A missing file is simply re-rendered from the preparation. |
| Career seed | `data/career_seed.json` | Initial import only; the Evidence Graph in the database is the runtime truth. |
| Configuration | `.env` **without** secrets: everything except `API_KEY`, `GEMINI_API_KEY`, `FIRECRAWL_API_KEY`, database passwords | Keep secrets in your password manager, not in the backup. |

Never back up: API keys, browser profiles or cookies, extension storage (it
holds the API key), tokens. The server never stores any of these.

Restore: put the database file back (or `pg_restore`), run
`alembic upgrade head` (a no-op when the backup is current, otherwise it
brings the schema forward), restore `DOCUMENTS_ROOT`, start the app; startup
recovery settles anything that was in flight at backup time. Verify with
`GET /health` and `GET /api/v1/ops/diagnostics`. `tests/hardening/
test_backup_restore.py` restores a representative synthetic database and
checks the invariants hold afterwards.

The AI cache (`.cache/ai/`) is disposable: keys are content-addressed and
entries are re-fetched on a miss.

## 6. Retention

| Data | Keep | Purge |
|---|---|---|
| `audit_events`, `application_events`, `outcome_events`, `signal_attributions`, `learning_snapshots` / `_metrics` / `_recommendations` | indefinitely (history and evidence) | never automatically |
| `signal_observations` | indefinitely (provenance of repeats) | only with the signal it belongs to |
| `signals.excerpt` | `SIGNAL_EXCERPT_RETENTION_DAYS` (180) for settled signals | `POST /api/v1/signals/purge-excerpts`; hashes, classification, attribution and outcomes stay |
| `execution_runs.diagnostics`, `form_snapshots` / `form_fields` | with the run (no page HTML is stored) | with the attempt |
| `document_artifacts` files | current version indefinitely; invalidated versions until you delete them | `POST /api/v1/documents/{id}/invalidate` marks; files may be removed by hand once no run references them |
| `.cache/ai/` | as long as useful | `POST /api/v1/ai/cache/clear` or delete the directory |
| `ai_usage` | indefinitely (cost accounting; no prompts) | never automatically |
| `discovery_runs`, `source_health` | indefinitely | never automatically |

Nothing required for audit or attribution history is ever deleted by the
application.

## 7. Index review (Phase 12)

Hot queries and the indexes that serve them (all present, asserted by
`test_migrations_chain.py::test_critical_constraints_and_indexes_exist`):

* queue claim scan `(tenant_id, state, available_at, priority)`; lane views
  `(tenant_id, lane, state)`; lookup by `idempotency_key`.
* attempts by tenant + status / cap period; unique `(tenant_id,
  opportunity_id)` and `submission_key`.
* execution runs by `application_id`, `(tenant_id, status)`,
  `(tenant_id, started_at)`; unique `idempotency_key`.
* candidate opportunities by tenant + state / band / eligibility / priority /
  scheduler code; unique `(tenant_id, opportunity_id)`.
* signals by `(tenant_id, status | application_id | observed_at | source |
  category | source_reference)`; unique `(tenant_id, dedupe_key)`.
* outcome events `(tenant_id, application_id, sequence)`; unique dedupe key;
  application outcomes `(tenant_id, current_outcome)`, `(tenant_id,
  needs_review)`.
* learning snapshots `(tenant_id, generated_at)`; metrics `(snapshot_id,
  dimension, metric)`.
* jobs by `canonical_key`, `normalized_source_url`, `normalized_application_url`,
  `content_hash`, `source_identifier`, status columns.

No new index was added in Phase 12: every measured query is served, and an
extra index would only add write cost on the high-volume ingestion path.

## 8. SQLite and PostgreSQL

| Concern | SQLite (local default) | PostgreSQL (CI, optional) |
|---|---|---|
| Concurrent writers | single writer, WAL, busy timeout; guarded `UPDATE`s detect a lost race | row locks; `SELECT … FOR UPDATE SKIP LOCKED` on the queue claim |
| Cap ledger | the conditional `UPDATE` serialises on the single writer | row lock on the ledger row |
| Unique constraints, savepoints, JSON | identical behaviour (`begin_nested` is used for every insert that may collide) | identical |
| Timestamps | naive UTC in both (`to_db` / `from_db` are the only conversions) | `TIMESTAMP WITHOUT TIME ZONE` |
| Thread tests | `tests/hardening/test_concurrency.py` runs real threads with retry on `database is locked` | the same tests plus the two PostgreSQL-only parallel-worker tests (`tests/scheduler/test_caps.py`, `tests/execution/test_identity_tenancy_concurrency.py`) |

The CI matrix runs the whole suite on both. SQLite is fully supported for a
single local worker plus the extension; run more than one Playwright worker
only on PostgreSQL.

## 9. Security review summary (Phase 12)

* Authentication: one `API_KEY`, constant-time comparison, never logged;
  dashboard cookie is HttpOnly + SameSite=Lax. Every `/api/*` write route
  and every `/dashboard/*` route is guarded (asserted by tests). A write is
  authorized by the `X-API-Key` header (API clients, the extension —
  unchanged) or, for the desktop shell's own pages, by the dashboard cookie
  **and** the exact header `X-Requested-With: careeros-desktop` (the header
  is request-context / CSRF protection, never a credential; a cookie alone
  never writes; requests whose `Origin` / `Sec-Fetch-Site` say cross-site are
  refused; CORS does not allow that header from any origin). The desktop
  pages under `/desktop/*` are dashboard-cookie gated like `/dashboard/*`
  (asserted by tests); `/desktop/documents/{id}/file` serves the
  candidate's own rendered document inline to that session.
* Authorization: the tenant is resolved server-side; every object lookup is
  tenant-filtered; the legacy `/api/v5/applications` routes were the one gap
  (returned rows regardless of tenant) and are now scoped (Phase 12).
* CORS: only `chrome-extension://` / `moz-extension://` origins, no wildcard,
  no credentials.
* Extension: MV3, permissions `storage activeTab scripting tabs alarms`,
  host permissions only for the local server, content script injected on
  demand, message listeners accept only the extension's own sender (Phase
  12), no cookie / history / clipboard / webRequest access, the API key
  travels in a header and is never logged.
* Files: identifier-only path components, resolved inside the root,
  write-once, hash-verified before upload.
* Logging: redaction filter on every handler; no page HTML, prompts, email
  bodies or document text at INFO.
* AI: exactly one boundary (`app/ai/providers.py` holds the only model HTTP
  calls); credential-shaped prompts refused; candidate data only to allowed
  providers; tenant-private cache for candidate scope.
* Database: no string-formatted SQL; every tenant table filtered.

## 10. Real-world validation (Phase 13)

`tools/validation/phase13_validate.py` runs the whole pipeline against real
public ATS boards and pages **without submitting anything**: a scratch
database and documents root, AI off, one page load at a time with a pacer,
discovery (`executor.prepare`) → server-side mapping (`capture_form`) →
document verification → pre-submit gate, never `execute`. Results land in
`docs/validation/phase13/*.json`; `docs/validation/phase13/REPORT.md` is the
written-up compatibility matrix. Re-run it after any change to discovery,
strategies, the page scripts or the adapters; it is the only evidence that
is not a fixture. What it found (and what changed) is summarised in
`docs/PROJECT_STATE.md` → "Completed in Blueprint Phase 13".

Operational rules that came out of it:

* A Greenhouse / Lever / Ashby application page may take several seconds
  after the load event to show its form (client rendering, embedded frame):
  `PLAYWRIGHT_SETTLE_MS` bounds that wait; a page that never shows an
  application form is an `AMBIGUOUS_FORM` handoff, not a failure.
* Real boards carry *invisible* challenge widgets (Lever: hCaptcha in
  invisible mode on every form; Greenhouse-hosted boards: a reCAPTCHA
  badge). They are reported in the run's diagnostics (`captcha_invisible`)
  but are not a wall — the provider scores the submit itself; a challenge
  shown after the click is a handoff after the click. A *visible* widget or
  challenge text hands off before anything is typed, and only a person can
  clear it (the provider's response token is detected, never produced).
* Bot protection on real boards is intermittent (an Ashby page showed a
  challenge on a later visit only): keep `PLAYWRIGHT_MIN_DELAY_SECONDS`
  generous and expect some `CAPTCHA_REQUIRED` handoffs on any board.
* Required fields real boards ask for that the profile does not hold
  (country, city, current company, LinkedIn URL, EEO questions, custom
  screening questions) surface as `NEEDS_USER_INPUT`; answer them once and
  the answer bank reuses them.
* The first real submissions are made one at a time from the desktop
  control center (Increment 4), not by a batch worker: Review → `Dry run` /
  `Submit for real` → the confirmation screen (target URL, document hashes,
  every answer, the preconditions) where you type `SUBMIT` → one headed
  Playwright run of that one attempt through the normal claim / gate /
  execute path → the result screen. The visible window is the audit trail;
  `PLAYWRIGHT_HANDOFF_WAIT_SECONDS` is how long it waits for you on a
  CAPTCHA, login or MFA handoff. An `UNKNOWN` outcome is never resubmitted
  — confirm or reject it from that screen once you have checked the
  employer's confirmation.
* The desktop shell announces what needs you (Increment 5): a Windows toast
  (`winotify`, part of the `desktop` extra) and the in-app *Notifications*
  list for READY, handoffs (CAPTCHA / login / MFA / unsupported form /
  missing answer), submitted, verified / uncertain, failed, and interview /
  rejection / recruiter / assessment signals. They are derived from the
  existing rows every 15 s and remembered in `DESKTOP_STATE_DIR/notifications.json`
  (watermarks and ids only; delete it to start fresh at *now*). A toast
  never carries an answer, a document, a URL, an error message or a key;
  clicking one only opens the page in the CareerOS window. Nothing is ever
  started, retried or resubmitted by a notification.
