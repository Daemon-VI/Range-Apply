# CareerOS Project State

## Current Phase

**Phase 3 — Job Intelligence, Eligibility & Fit Scoring**

## Status

**Phase 1 & 2: COMPLETE.** **Phase 3: implemented and wired end to end** — the
matching engine runs against real discovered jobs (not just fixtures), is
reachable through `/api/v3/matches/*`, and is rendered in the dashboard
shortlist. A round of P0 correctness fixes also landed across Phase 2
(dedup identity, URL normalization, timezone handling, run recovery, rate
limiting) and Phase 1 (see Completed below).

## Completed

### Phase 1 — Career Brain Foundation
- [x] Repository structure established
- [x] RIBHU_CAREER_CONTEXT.md (canonical context)
- [x] Career Context Pack (`docs/career/`)
- [x] Project documentation (`docs/projects/`)
- [x] PROFILE.md, MASTER_RESUME.md, SKILLS.md, EXPERIENCE.md
- [x] ACHIEVEMENTS.md, PREFERENCES.md, TRUTH_RULES.md, CAREER_BRAIN.md
- [x] PROJECT_INDEX.md and individual project files
- [x] Pydantic data models (Profile, Skill, Project, Experience, Achievement, Preference, CareerFact, Claim)
- [x] TruthValidator with verification status enforcement
- [x] CareerBrainService with full interface
- [x] FastAPI application with all required endpoints
- [x] Structured seed data (`data/career_seed.json`)

### Phase 2 — Job Discovery, Web Intelligence & Normalization
- [x] Domain models (`RawJob`, `NormalizedJob`, `GraduationRequirement`, `SourceReference`, `DiscoveryRun`, enums)
- [x] PostgreSQL & SQLite schema via SQLAlchemy ORM models, **owned exclusively by Alembic**
  (the app fails fast with `SchemaNotReadyError` instead of ever creating tables itself)
- [x] Alembic migrations: `create_jobs_tables`, `identity_dates_and_match_provenance`, `create_phase3_tables`
- [x] Source adapter framework (`JobSource` ABC) with shared retry/backoff and rate limiting
  (`fetch_json` in `app/jobs/sources/base.py`, `app/jobs/ratelimit.py`)
- [x] Greenhouse, Lever, and Ashby source adapters (public JSON APIs), now carrying
  `source_posted_at` / `source_updated_at` / `source_deadline`
- [x] Content fetcher layer (`HttpFetcher`, `FirecrawlFetcher` — selective, never required for the ATS path)
- [x] Deterministic extraction layer (skills, technologies, graduation years, remote, employment type, salary)
- [x] Normalization layer (`JobNormalizer`, title cleaner, canonical key, SHA-256 content hashing)
- [x] Deduplication layer — 5-level identity hierarchy, **exact-equality only** (the previous
  `LIKE '<url>%'` prefix match that could merge `/jobs/123` into `/jobs/1234` is gone)
- [x] LLM fallback layer (`LLMProvider` ABC, `StubProvider`, `GeminiProvider`, on-disk
  content-hash `CachingLLMProvider`, `LLMFallbackExtractor`)
- [x] Discovery pipeline (`JobDiscoveryService`) — discover -> normalize -> LLM-enrich-if-needed
  -> deduplicate -> persist, per-job SAVEPOINT so one bad job cannot poison a run
- [x] **Discovery moved to background execution.** `/api/v2/discovery/run` and
  `/discovery/run-batch` return `202 Accepted` and run via `BackgroundTasks` by default
  (`?wait=true` still supports synchronous small-board calls); `run_many` bounds
  concurrency with `DEFAULT_DISCOVERY_CONCURRENCY` and rate-limits per source
- [x] Stale-run recovery (`reconcile_stale_runs`) — a run stuck at `running` after a crash
  is reconciled to `interrupted` on next startup/run
- [x] Stale-job sweep (`should_sweep` / `close_missing_jobs`) — scoped to `(source,
  source_identifier)`, refuses to run on an incomplete or empty observed set
- [x] Timezone correctness (`app/core/timeutils.py`) — aware-UTC domain models,
  naive-UTC database columns, one sanctioned boundary
- [x] FastAPI routes under `/api/v2/` (`/jobs`, `/jobs/{id}`, `/jobs/stats/summary`,
  `/discovery/runs`, `/discovery/run`, `/discovery/run-batch`)
- [x] Internal HTML dashboard (`/dashboard/`, `/dashboard/jobs`, `/dashboard/jobs/{id}`, `/dashboard/runs`)
- [x] Phase 2 documentation (`docs/jobs/*`)

### Phase 3 — Job Intelligence, Eligibility & Fit Scoring
- [x] **Adapter bridging discovery and intelligence** (`job_row_to_normalized` in
  `app/intelligence/adapters/job_adapter.py`) — the piece that was previously missing,
  which meant Phase 3 could only ever run against hand-built test fixtures
- [x] Curated local skill taxonomy with alias resolution and disambiguation context patterns
  (`app/intelligence/taxonomy/skills.py`), replacing a ~60-keyword dictionary with false-positive
  matches (`\bgo\b`, `\brest\b`, `\bspring\b`)
- [x] Section-aware, deterministic requirement extraction (`RequirementExtractor`) — no LLM
  call in this layer at all
- [x] Layered skill matching (`SkillMatcher`: exact / alias / adjacent / lexical, plus an
  optional CPU-only semantic layer behind the `semantic` extra, off by default)
- [x] Hard-gate eligibility engine (`EligibilityEngine.evaluate_detailed`) — preserves
  `UNCERTAIN` as a real, distinct outcome and derives confidence rather than hardcoding it
- [x] Evidence resolution against the Career Brain (`EvidenceResolver`) — skills, project
  technologies, work-experience technologies, degree level, total experience duration
- [x] Soft preference evaluation (`PreferenceEvaluator`) — never blocks eligibility
- [x] Policy-weighted fit scoring (`FitScoringEngine`, `POLICY_VERSION`/`ENGINE_VERSION`
  recorded per match so a policy change never silently reinterprets an old score)
- [x] Explanation generation (`ExplanationGenerator`) — strengths/gaps/explanation text, not
  just a number
- [x] `MatchOrchestrator` wiring all of the above into one `JobMatch` per job;
  `build_orchestrator()` is the single assembly point shared by the API, background
  tasks, and the dashboard
- [x] Match persistence (`run_matching`, `MatchPolicyRow`/`MatchRunRow`/`JobMatchRow`/
  `RequirementAssessmentRow`) — the tables existed since the Phase 3 migration but nothing
  wrote to them before; they are populated end to end now
- [x] Real `/api/v3/matches/*` routes: list (filter/sort/paginate), per-job detail with full
  requirement-assessment audit trail, run history, and `recalculate` (background by default,
  `?wait=true` for synchronous, `only_stale` to rescore only changed jobs)
- [x] Shortlist dashboard UI (`/dashboard/shortlist`, `/dashboard/shortlist/{job_id}`) —
  ranked, filterable, shows eligibility/uncertainty and per-requirement evidence

## In Progress

Nothing currently in progress. Phase 3's core engine is wired end to end; remaining
Phase 3-adjacent polish (policy tuning, more requirement categories, taxonomy coverage
gaps found in real job postings) is picked up opportunistically rather than tracked as
open work here.

## Known Issues

- **P4 (resume/application tailoring) has not started.** No dynamic resume generation,
  no per-job tailoring logic exists yet.
- **P5 (application engine, Playwright submission) has not started.** Nothing in this
  repo submits an application; `application_url` is surfaced for the human to act on.
- **P6 (scaling: Redis/queue, distributed rate limiting) has not started and is not
  currently justified** — see `docs/IMPLEMENTATION_CONTEXT.md` §6 for why it's
  deliberately deferred rather than missing.
- **No PostgreSQL deployment exists yet.** The schema and Alembic migrations support it
  (`postgres` extra, `DATABASE_URL=postgresql://...`), and CI now runs the suite against a
  real Postgres service container, but there is no running Postgres deployment — SQLite is
  what actually runs today.
- **Semantic skill matching is unproven and off by default.** `SEMANTIC_MATCHING_ENABLED`
  defaults to `false`; the CPU-only sentence-transformers layer exists and is wired in as
  the lowest-priority matching layer, but has not been evaluated against real job postings
  for accuracy or latency impact.
- **`app/intelligence/services/requirement_interpreter.py` is legacy** — present in the
  codebase but not part of the active `MatchOrchestrator` wiring.
- Contact info, GitHub, LinkedIn, portfolio URLs in Career Brain marked as NEEDS_REVIEW (user input pending)
- Ticket Engine performance metrics are project-reported
- Live Firecrawl requires `FIRECRAWL_API_KEY` for private/unstructured pages; structured ATS adapters are completely free

## Architecture Decisions

1. **Structured ATS APIs First** — Greenhouse, Lever, and Ashby provide free, unauthenticated, structured JSON feeds with full HTML JDs, saving 100% of Firecrawl quota for these sources.
2. **Deterministic First, LLM Second** — Pattern matching and regex extract 95%+ of fields reliably in Phase 2; Phase 3's `RequirementExtractor` never calls an LLM at all. LLMs are only invoked for fields deterministic extraction left ambiguous.
3. **5-Level Deduplication Hierarchy, exact-equality only** — Prevents duplicate job records while preserving multi-source provenance and versioning meaningful JD updates; no prefix or fuzzy matching at any level.
4. **Clean Domain / Persistence Separation** — Pydantic models for domain logic and API schemas; SQLAlchemy ORM models for database persistence; `job_row_to_normalized` is the one bridge between them for Phase 3.
5. **Alembic owns the schema** — the application never calls `create_all()` outside tests; it fails fast with `SchemaNotReadyError` if the database hasn't been migrated.
6. **No Redis Yet** — the discovery and matching pipelines are in-process, SAVEPOINT-batched, and rate-limited per-process; deferred to a later scaling phase and only once a measured workload justifies it. See `docs/IMPLEMENTATION_CONTEXT.md` for the full free-tier architecture rationale.
7. **Curated local skill taxonomy over ESCO/O*NET/Lightcast** — evaluated and rejected for now (occupation-centric coverage / hosted API key requirement); `TaxonomyProvider` is the seam to swap later.

## Context Files

| File | Purpose |
|---|---|
| `RIBHU_CAREER_CONTEXT.md` | Canonical career context |
| `docs/PROJECT_STATE.md` | Operational project memory |
| `docs/IMPLEMENTATION_CONTEXT.md` | Module map, data flow, invariants, extension points |
| `docs/ARCHITECTURE.md` | System architecture |
| `docs/DEVELOPMENT.md` | Developer setup guide |
| `docs/career/*.md` | Career Context Pack |
| `docs/projects/*.md` | Project documentation |
| `docs/jobs/*.md` | Phase 2 job discovery & pipeline docs |
| `data/career_seed.json` | Career Brain seed data |

## Last Updated

September 10, 2026
