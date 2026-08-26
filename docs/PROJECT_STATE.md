# CareerOS Project State

## Current Phase

**Phase 2 — Job Discovery, Web Intelligence & Normalization**

## Status

**COMPLETE** — All Phase 1 and Phase 2 deliverables implemented and tested.

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
- [x] Python 3.9 type hint syntax clean

### Phase 2 — Job Discovery, Web Intelligence & Normalization
- [x] Domain models (`RawJob`, `NormalizedJob`, `GraduationRequirement`, `SourceReference`, `DiscoveryRun`, enums)
- [x] PostgreSQL & SQLite schema via SQLAlchemy ORM models
- [x] Alembic migrations configured and initial migration generated (`151a105ef8d5_create_jobs_tables.py`)
- [x] Source adapter framework (`JobSource` ABC)
- [x] Greenhouse source adapter (`boards-api.greenhouse.io` public JSON API)
- [x] Lever source adapter (`api.lever.co` public JSON API)
- [x] Ashby source adapter (`api.ashbyhq.com` public JSON API)
- [x] Content fetcher layer (`HttpFetcher`, `FirecrawlFetcher` with retries & backoff)
- [x] Deterministic extraction layer (skills, technologies, graduation years, remote, employment type, salary)
- [x] Normalization layer (`JobNormalizer`, title cleaner, canonical key, SHA-256 content hashing)
- [x] Deduplication layer (`JobDeduplicator` 5-level deterministic identity hierarchy, cross-source provenance, change versioning)
- [x] LLM fallback layer (`LLMProvider` ABC, `StubProvider`, `GeminiProvider`, `LLMFallbackExtractor`)
- [x] Discovery pipeline (`JobDiscoveryService` orchestrating discover -> extract -> normalize -> deduplicate -> persist)
- [x] FastAPI routes under `/api/v2/` (`/jobs`, `/jobs/{id}`, `/jobs/stats/summary`, `/discovery/runs`, `/discovery/run`)
- [x] Internal HTML dashboard (`/dashboard/`, `/dashboard/jobs`, `/dashboard/jobs/{id}`, `/dashboard/runs`)
- [x] Modern FastAPI lifespan handler replacing deprecated startup event
- [x] Comprehensive test suite (90 passing tests covering Phase 1 + Phase 2)
- [x] Phase 2 documentation (`docs/jobs/*`)

## In Progress

Nothing — Phase 2 complete.

## Next (Phase 3 Recommendations)

1. **Job Description Intelligence**: Deep semantic parsing of extracted requirements against Career Brain.
2. **Eligibility Engine**: Strict graduation year (2027), work authorization, and location eligibility filtering.
3. **Relevance & Fit Scoring**: Scoring jobs (P1, P2, P3, IGNORE) based on Ribhu's project strengths (e.g. Ticket Engine concurrency, Python/Go backend).
4. **Match Explanation**: Explaining why a job matches and which projects/skills to highlight.

## Known Issues

- Contact info, GitHub, LinkedIn, portfolio URLs in Career Brain marked as NEEDS_REVIEW (user input pending)
- Ticket Engine performance metrics are project-reported
- Live Firecrawl requires `FIRECRAWL_API_KEY` for private/unstructured pages; structured ATS adapters are completely free

## Architecture Decisions

1. **Structured ATS APIs First** — Greenhouse, Lever, and Ashby provide free, unauthenticated, structured JSON feeds with full HTML JDs, saving 100% of Firecrawl quota for these sources.
2. **Deterministic First, LLM Second** — Pattern matching and regex extract 95%+ of fields reliably; LLMs are only invoked for ambiguous fields.
3. **5-Level Deduplication Hierarchy** — Prevents duplicate job records while preserving multi-source provenance and versioning meaningful JD updates.
4. **Clean Domain / Persistence Separation** — Pydantic models for domain logic and API schemas; SQLAlchemy ORM models for database persistence.
5. **No Redis Yet** — Initial async pipeline is clean and extensible; Redis can be dropped in later without modifying domain logic.

## Context Files

| File | Purpose |
|---|---|
| `RIBHU_CAREER_CONTEXT.md` | Canonical career context |
| `docs/PROJECT_STATE.md` | Operational project memory |
| `docs/ARCHITECTURE.md` | System architecture |
| `docs/DEVELOPMENT.md` | Developer setup guide |
| `docs/career/*.md` | Career Context Pack |
| `docs/projects/*.md` | Project documentation |
| `docs/jobs/*.md` | Phase 2 job discovery & pipeline docs |
| `data/career_seed.json` | Career Brain seed data |

## Last Updated

August 21, 2026
