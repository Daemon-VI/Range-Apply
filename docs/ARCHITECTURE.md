# CareerOS Architecture

**Last updated:** September 2026

---

## Phase 1, 2 & 3 — IMPLEMENTED

```text
FastAPI Application (/api/v1/ & /api/v2/ & /api/v3/ & /dashboard/)
       │
       ├── Career Brain Layer (Phase 1)
       │       ↓
       │   CareerBrainService  ──────────────────────►  single source of truth
       │       ↓                                        for every candidate fact
       │   TruthValidator
       │       ↓
       │   Structured Career Data (JSON seed / Pydantic Models)
       │
       ├── Job Discovery & Ingestion Layer (Phase 2)
       │       ↓
       │   JobDiscoveryService  (background by default; BackgroundTasks + rate limiting)
       │       ↓
       │   Source Adapters (Greenhouse / Lever / Ashby)  +  Firecrawl (selective fallback)
       │       ↓            [shared retry/backoff + per-source token-bucket rate limit]
       │   Deterministic Extraction & Normalization
       │       ↓
       │   LLM Fallback (through the AI Gateway; only for fields left UNKNOWN; off by default)
       │       ↓
       │   JobDeduplicator (5-Level exact-match identity hierarchy)
       │       ↓
       │   JobRow  (SQLAlchemy ORM + PostgreSQL / SQLite, schema owned by Alembic)
       │       ↓
       │   Stale-job sweep (scoped to source+identifier, only after a healthy run)
       │
       └── Job Intelligence & Matching Layer (Phase 3)
               ↓
           job_row_to_normalized (JobRow -> NormalizedJob; pure field mapping, no re-extraction)
               ↓
           RequirementExtractor (section-aware, deterministic — no LLM call)
               ↓
           EligibilityEngine (hard gates: graduation year, work auth, location — UNCERTAIN preserved)
               ↓
           EvidenceResolver (Career Brain skills / projects / experience, via SkillMatcher)
               ↓
           PreferenceEvaluator (soft signals — never blocks eligibility)
               ↓
           FitScoringEngine (policy-weighted component scoring; POLICY_VERSION recorded per match)
               ↓
           ExplanationGenerator (strengths / gaps / explanation text)
               ↓
           JobMatchRow + RequirementAssessmentRow  (persisted, queryable, reproducible)
               ↓
           /api/v3/matches/* + /dashboard/shortlist
```

### Components

| Component | Status | Description |
|---|---|---|
| Career Context Pack | IMPLEMENTED | Markdown docs in `docs/career/` and `docs/projects/` |
| Career Models | IMPLEMENTED | Profile, Skill, Project, Experience, Achievement, Preference, CareerFact, Claim |
| CareerBrainService | IMPLEMENTED | Programmatic career information interface; the single source of truth Phase 3 reads against |
| TruthValidator | IMPLEMENTED | Verification status enforcement |
| Job Domain Models | IMPLEMENTED | `RawJob`, `NormalizedJob`, `GraduationRequirement`, `SourceReference`, `DiscoveryRun` |
| Database Layer | IMPLEMENTED | SQLAlchemy ORM + Alembic migrations, **schema-authoritative**: the app fails fast (`SchemaNotReadyError`) rather than ever creating tables itself |
| Source Adapters | IMPLEMENTED | `GreenhouseSource`, `LeverSource`, `AshbySource` (unauthenticated, structured JSON), carrying `source_posted_at`/`source_updated_at`/`source_deadline` |
| Rate Limiting & Retry | IMPLEMENTED | Per-source token bucket (`app/jobs/ratelimit.py`) + bounded exponential backoff with `Retry-After` support, shared by every adapter via `JobSource.fetch_json` |
| Web Fetchers | IMPLEMENTED | `HttpFetcher`, `FirecrawlFetcher` — Firecrawl is selective, never required for the ATS path, never used for submission |
| Extraction & Normalization | IMPLEMENTED | `JobNormalizer`, deterministic regex/metadata extractors, SHA-256 content hashing |
| Deduplication | IMPLEMENTED | 5-level **exact-equality** identity hierarchy, provenance tracking, change versioning |
| LLM Fallback | IMPLEMENTED | `LLMProvider` ABC, `StubProvider`, `GatewayLLMProvider` (over the Phase 8b AI Gateway: provider, versioned cache, budget, schema), `LLMFallbackExtractor` — invoked only for fields deterministic extraction left ambiguous, and only with `AI_ENABLED=true` |
| AI Gateway (Phase 8b) | IMPLEMENTED | `app/ai/`: one `AIGateway.run(AIRequest) -> AIResponse` for every model call; providers stub / Gemini / local Ollama; content-addressed versioned cache (tenant-private for candidate data); per-run call budgets; schema validation; credential-shaped prompts refused; `ai_usage` accounting; `/api/v1/ai/*`; off by default, every caller falls back deterministically |
| Tier-1 gates (Phase 8b) | IMPLEMENTED | `app/pipeline/gates.py`: ordered, versioned (`tier1-gates-v1`), all-reporting static gate set mapped onto the existing `AdmissionReason` codes; `evaluate_admission` derives from it; versions recorded on candidate opportunities and audit events; `/api/v1/policy/{fit-bands,gates}` |
| Hardening (Phase 12) | IMPLEMENTED | Startup recovery for lost execution runs (`app/execution/recovery.py`), expired-lease refusal at the final gate, atomic signal observation counter, policy first-touch race guard, tenant-scoped legacy v5 routes, extension sender validation, operational diagnostics (`/api/v1/ops/diagnostics`, `/dashboard/ops`), `tests/hardening/` (concurrency, execution safety, caps, recovery, security, failure injection, migrations chain, backup/restore, soak), `docs/OPERATIONS.md` |
| Learning Engine (Phase 11) | IMPLEMENTED | `app/learning/`: time-aware, evidence-aware learning dataset (`features-v1`); beta-binomial shrinkage toward the tenant baseline + Wilson intervals + sample-size confidence; per-dimension observed rates and response times; immutable versioned snapshots; associational recommendations with sample size and caveat; optional `expected_response` → `learned_prior` (ordering only, opt-in); no policy mutation, no top-N; `/api/v1/learning`, `/dashboard/learning`; zero AI |
| Signal Inbox (Phase 10) | IMPLEMENTED | `app/signals/`: idempotent tenant-scoped ingestion (stable reference or content hash; repeats are observations), deterministic rule classifier with versioned precedence, optional AI classification only through the gateway (`CLASSIFY_SIGNAL`, capped at MEDIUM), deterministic attribution (strong ids → thread → reference → URL → company/title; AMBIGUOUS / UNMATCHED instead of guesses), append-only attribution and outcome histories with evidence strength, derived current status (`outcome-rules-v1`), execution results as signals, review actions, `/api/v1/signals`, `/dashboard/signals` |
| Discovery Execution | IMPLEMENTED | Background by default (`BackgroundTasks`), bounded concurrency, stale-run recovery on crash (`reconcile_stale_runs`), stale-job sweep (`close_missing_jobs`) |
| Job-Row → Domain Adapter | IMPLEMENTED | `job_row_to_normalized` — the bridge that lets Phase 3 run against real discovered jobs |
| Skill Taxonomy | IMPLEMENTED | Curated local taxonomy with alias + disambiguation-context matching (`app/intelligence/taxonomy/skills.py`), behind a `TaxonomyProvider` seam |
| Requirement Extraction | IMPLEMENTED | Section-aware `RequirementExtractor`, fully deterministic |
| Skill Matching | IMPLEMENTED | 4 deterministic layers + optional CPU-only semantic layer (off by default, `semantic` extra) |
| Eligibility Engine | IMPLEMENTED | Hard gates with a real `UNCERTAIN` outcome and derived (not hardcoded) confidence |
| Evidence Resolution | IMPLEMENTED | Skills, project technologies, experience technologies, degree level, experience duration, all read from the Career Brain |
| Preference Evaluation | IMPLEMENTED | Soft ranking signals, independent of eligibility |
| Fit Scoring | IMPLEMENTED | Policy-weighted component scoring, versioned (`POLICY_VERSION`/`ENGINE_VERSION`) |
| Explanation Generation | IMPLEMENTED | Human-readable strengths/gaps/explanation per match |
| Match Persistence | IMPLEMENTED | `MatchPolicyRow`/`MatchRunRow`/`JobMatchRow`/`RequirementAssessmentRow`, SAVEPOINT-batched runs |
| REST API | IMPLEMENTED | `/api/v1/` career brain + `/api/v2/` jobs discovery + `/api/v3/matches` intelligence |
| Internal Dashboard | IMPLEMENTED | Jinja2 server-rendered views at `/dashboard/` including the ranked shortlist and per-job match detail |
| CI / Scheduled Discovery | IMPLEMENTED | GitHub Actions: test suite on SQLite + PostgreSQL service container, plus a free-tier cron workflow driving `/api/v2/discovery/run-batch` and `/api/v3/matches/recalculate` |
| Test Suite | IMPLEMENTED | Tests spanning career brain, job models/database/adapters/API, and the Phase 3 intelligence engine |

---

## Phase 4 / 5 / 6 — LITE (implemented, being extended)

```text
       ├── Tailoring Layer (Phase 4, lite)
       │       TailoringEngine (deterministic; every sentence formatted from Career Brain
       │       evidence selected via evidence_selector) -> TailoredArtifactRow (versioned,
       │       evidence_refs, approved flag) -> /api/v4/tailoring/*
       │
       └── Application Layer (Phase 5 + 6-lite)
               ApplicationEngine (state machine DISCOVERED..SUBMITTED/FAILED; attempts keyed
               by tenant + opportunity; order in submit(): kill switch -> idempotency ->
               explicit approval -> adapter)
               ATSAdapter contract + GreenhousePlaywrightAdapter (dry-run only today)
               KillSwitchRow (global + per-source pause) -> /api/v5/applications/*
```

## Blueprint Phases 1–5 (implemented)

```text
Career Brain (Phase 1)   evidence_nodes with grades + provenance, variants, answer bank, audit
        │
Discovery (Phase 3)      raw-hash fast path, circuit breaker, source health, reposts, capture
        │
Opportunities (Phase 2)  shared opportunities + tenant candidate_opportunities, eligibility
        │                decisions, priority (order only), application_policies, DB queue
        │
Scheduler (Phase 5)      decide() per candidate opportunity with one AdmissionReason;
        │                caps via application_cap_ledger (attempts, tenant-local day/week),
        │                normalised company cool-down + blocklist, duplicates/reposts,
        │                priority-ordered idempotent PREPARE enqueueing, scheduler_runs
        │
Preparation (Phase 4)    evidence-backed packages (L0/L1 zero-AI, L2 optional), TruthValidator
        │                gate, NEEDS_USER_INPUT, READY -> attempt READY (execution-ready)
        │
Execution (Phase 6)      ExecutionPackage (tenant / candidate opportunity / opportunity /
                         preparation / attempt), SUBMIT items on the same queue, submission-time
                         preconditions (stale, blocklist, cool-down, duplicates, caps), guarded
                         READY->SUBMITTING mutex, Executor contract (MOCK, MANUAL,
                         PLAYWRIGHT_LOCAL, BROWSER_EXTENSION), verification, human handoff
```

```text
Documents (Phase 8)      DocumentService: READY preparation blocks -> DocumentModel -> fpdf2 PDF /
        │                python-docx DOCX (same content, layout only) -> validate (parses, pages,
        │                every content line present) -> immutable versioned file under
        │                data/documents/<tenant>/<co>/<prep>/ + document_artifacts row (SHA-256,
        │                input fingerprint, renderer version); unchanged input reuses the file
        │
Local worker (Phase 7)   python -m app.execution.worker  ->  ExecutionService.run_queue
        │                PlaywrightExecutor: one Chromium on the candidate's laptop, page per
        │                attempt, DOM discovery -> FormSnapshot, typed filling from prepared
        │                answers, uploads the preparation's rendered PDF (hash re-checked at the
        │                upload), pre-submit gate (incl. cap refresh), click, classify,
        │                verify from page evidence; CAPTCHA / login / MFA / unsupported form
        │                -> BLOCKED handoff; dry-run by default
        └── strategies   greenhouse | lever | ashby (standard controls only) | generic
```

```text
Extension (Phase 9)      extension/ (MV3, vanilla JS) in the candidate's own browser:
        │                tab URL -> /extension/match -> /extension/claim -> start(BROWSER_EXTENSION)
        │                -> discover.js (same script as the Playwright runner) -> form (server maps)
        │                -> documents/{id}/file?for_upload=true (SHA-256 re-checked) -> fill
        │                -> the person's or the extension's submit is intercepted -> /extension/gate
        │                (all preconditions, submit_invoked) -> click -> page evidence -> result
        │                -> BrowserExtensionExecutor.verify; CAPTCHA / login / MFA / no document ->
        └── popup        handoff, the person finishes and confirms; unanswered fields answered
                         from the popup through the answer bank; modes manual | auto | dry-run
```

```text
Signal Inbox (Phase 10)  app/signals/: execution results (Phase 6/7/9 verification), supplied
        │                emails, status-page / extension observations, manual notes
        │                -> SignalIngestionService.ingest (idempotent: reference or content hash;
        │                   repeats = observations; bounded redacted excerpt, no HTML/headers)
        │                -> classify (rules, versioned precedence; AI via the gateway only when
        │                   not confident, never above MEDIUM) -> attribute (strong ids, thread,
        │                   reference, URL, company+title; one match or AMBIGUOUS / UNMATCHED)
        │                -> outcome_events (append-only, STRONG / MODERATE / WEAK evidence)
        │                -> application_outcomes (derived: highest stage, terminal needs STRONG,
        │                   weak never downgrades, conflicts -> NEEDS_REVIEW)
        │                -> guarded lifecycle sync (INTERVIEWING / REJECTED) and, for a STRONG
        │                   confirmation of an UNCERTAIN attempt, ExecutionService.verify
        └── review       link / confirm / reject / ignore / merge / reprocess, audited;
                         /api/v1/signals, /dashboard/signals (full trace); no learning yet
```

```text
Learning (Phase 11)      app/learning/: applications + candidate opportunities + opportunities + jobs
        │                + preparations + execution runs + outcome events (observed by as_of only)
        │                -> LearningRow (features-v1: company / title / role family / band / source /
        │                   lane / level / cover letter / variant / executor / verification / labels
        │                   with evidence quality) -> aggregate per dimension (beta-binomial toward
        │                   the tenant baseline, Wilson interval, NONE/LOW/MEDIUM/HIGH confidence)
        │                -> learning_snapshots + learning_metrics + learning_recommendations
        │                   (versioned, never overwritten) -> dashboard / API
        └── ordering     expected_response (response + interview vs baseline, confidence-weighted)
                         -> learned_prior priority component ONLY when learning_settings.
                         ordering_enabled; never admission, never a cap, never a top-N
```

Both executors speak the same `Executor` contract; the extension does it over
`/api/v1/execution` (match → claim → start → form → gate → result | handoff →
verify | confirm) from a `chrome-extension://` origin allowed by CORS, with
the API key held in the extension's own storage. The scheduler never submits;
the execution service submits only through an executor and only after every
precondition holds again at that moment. No cloud browser, no browser fleet,
no server-side credentials: a persistent browser profile, if used, is a local
directory the server never reads.

## Target architecture — see `docs/BLUEPRINT.md`

The revised blueprint (§3) is the target: a progressive Tier 1–5 pipeline,
opportunity-level identity, tailoring levels with a blocking structural
validator, an AI gateway with caching/budgets/PII routing, a Postgres
application queue consumed by a browser extension and a local Playwright
runner, policy lanes, a signal inbox and a volume-neutral learning engine, all
on free-tier infrastructure. Phase 0 (foundation) is done; the module map in
`docs/IMPLEMENTATION_CONTEXT.md` tracks what exists.

Cross-cutting foundation (Phase 0):

- `app/core/errors.py` — structured error hierarchy mapped to HTTP by one handler.
- `app/core/logging.py` — root logging with secret redaction on every handler.
- `app/config.py` — `DEPLOYMENT_MODE` (solo | hosted), `DEFAULT_TENANT_ID`, Postgres pool settings.

## Deployment note

There is no hosted PostgreSQL deployment running today; SQLite is what actually runs in
development. `docker-compose.yml` starts a local Postgres 16 with pgvector, the schema and
Alembic migrations support it, and CI runs the whole suite against the same image, so moving
to it is a configuration change (`DATABASE_URL` + the `postgres` extra), not a code change.
