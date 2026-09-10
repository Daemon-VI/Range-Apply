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
       │   LLM Fallback (Stub / Gemini, only for fields left UNKNOWN, disk-cached)
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
| LLM Fallback | IMPLEMENTED | `LLMProvider` ABC, `StubProvider`, `GeminiProvider`, disk-cached `CachingLLMProvider`, `LLMFallbackExtractor` — invoked only for fields deterministic extraction left ambiguous |
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

## Phase 4+ — PLANNED (not started)

- **P4 — Resume/application tailoring.** Dynamic resume generation and per-job tailoring
  against the Career Brain and match assessments. No code exists for this yet.
- **P5 — Application engine.** Automated submission (Playwright browser workers), real-time
  monitoring & notifications. Not started; `application_url` is currently surfaced for a
  human to act on manually.
- **P6 — Scaling.** Redis/queue-backed background execution, distributed rate limiting,
  horizontal scaling beyond a single process. Deliberately deferred — see
  `docs/IMPLEMENTATION_CONTEXT.md` §6 for the free-tier rationale — and not started.

## Deployment note

There is no PostgreSQL deployment running today; SQLite is what actually runs in
development. The schema, Alembic migrations, and CI (a dedicated Postgres service-container
job) all support PostgreSQL so that moving to it is a configuration change
(`DATABASE_URL` + the `postgres` extra), not a code change.
