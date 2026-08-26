# CareerOS Architecture

**Last updated:** August 2026

---

## Phase 1 & 2 — IMPLEMENTED

```text
FastAPI Application (/api/v1/ & /api/v2/ & /dashboard/)
       ├── Career Brain Layer (Phase 1)
       │       ↓
       │   CareerBrainService
       │       ↓
       │   TruthValidator
       │       ↓
       │   Structured Career Data (JSON seed / Pydantic Models)
       │
       └── Job Discovery & Ingestion Layer (Phase 2)
               ↓
           JobDiscoveryService
               ↓
           Source Adapters (Greenhouse / Lever / Ashby / Firecrawl)
               ↓
           Deterministic Extraction & Normalization
               ↓
           LLM Fallback (Stub / Gemini)
               ↓
           JobDeduplicator (5-Level Hierarchy)
               ↓
           SQLAlchemy ORM + PostgreSQL / SQLite (Alembic)
```

### Components

| Component | Status | Description |
|---|---|---|
| Career Context Pack | IMPLEMENTED | Markdown docs in `docs/career/` and `docs/projects/` |
| Career Models | IMPLEMENTED | Profile, Skill, Project, Experience, Achievement, Preference, CareerFact, Claim |
| CareerBrainService | IMPLEMENTED | Programmatic career information interface |
| TruthValidator | IMPLEMENTED | Verification status enforcement |
| Job Domain Models | IMPLEMENTED | `RawJob`, `NormalizedJob`, `GraduationRequirement`, `SourceReference`, `DiscoveryRun` |
| Database Layer | IMPLEMENTED | SQLAlchemy ORM + Alembic migrations (`jobs`, `source_references`, `versions`, `runs`) |
| Source Adapters | IMPLEMENTED | `GreenhouseSource`, `LeverSource`, `AshbySource` (unauthenticated, structured JSON) |
| Web Fetchers | IMPLEMENTED | `HttpFetcher`, `FirecrawlFetcher` (retries, backoff, rate limiting) |
| Extraction & Normalization | IMPLEMENTED | `JobNormalizer`, deterministic regex/metadata extractors, SHA-256 content hashing |
| Deduplication | IMPLEMENTED | 5-level deterministic identity hierarchy, provenance tracking, change versioning |
| LLM Fallback | IMPLEMENTED | `LLMProvider`, `StubProvider`, `GeminiProvider`, `LLMFallbackExtractor` |
| REST API | IMPLEMENTED | `/api/v1/` career brain routes + `/api/v2/` jobs discovery routes |
| Internal Dashboard | IMPLEMENTED | Jinja2 server-rendered views at `/dashboard/` |
| Test Suite | IMPLEMENTED | 90 tests passing across career brain, job models, database, adapters, and API |

---

## Phase 3 — PLANNED

- Job Description Intelligence (semantic requirement parsing)
- Eligibility Engine (2027 graduation, work authorization, location)
- Relevance & Match Scoring (P1 / P2 / P3 / IGNORE)
- Match Explanation against Career Brain facts

---

## Phase 4+ — FUTURE

- Dynamic resume tailoring/generation
- Redis workers for background queue execution
- Browser workers (Playwright) for application submission
- Real-time monitoring & Telegram notifications
