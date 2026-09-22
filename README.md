# RangeApply

AI-assisted career platform focused on job discovery, job intelligence, and stepwise automation toward autonomous applications.

## 1) Project Overview

RangeApply (CareerOS) is built to reduce repetitive job-search work by combining:
- structured career context,
- automated job discovery/normalization,
- and an emerging match-intelligence layer.

The technically interesting part is the phased architecture: foundational truth-validated career data (Phase 1), production-style ingestion and deduplication (Phase 2), then match orchestration scaffolding (Phase 3 in progress).

## 2) Current Development Status

Implementation follows **`docs/BLUEPRINT.md`**, the revised CareerOS blueprint
(phases 0–13). Its objective: *maximise successful, truthful, relevant job
application throughput* on free-tier infrastructure, with AI optional and
deterministic processing first.

| PRD phase | Status | Summary |
|---|---|---|
| P1 Career Brain | ✅ Complete | Typed, truth-validated career data, service layer, v1 API |
| P1.5 Foundation | ✅ Complete | FastAPI, SQLAlchemy, Alembic-owned schema, SQLite/Postgres, dashboard, API-key auth |
| P2 Discovery | ✅ Complete | Greenhouse/Lever/Ashby adapters, Firecrawl fallback, deterministic extraction, 5-level exact dedup, versioning, v2 API + dashboard, scheduled runs |
| P3 Intelligence | ✅ Complete | Deterministic requirement extraction, eligibility with `UNCERTAIN`, evidence resolution, policy-weighted fit, explanations, v3 API, shortlist |
| P4 Tailoring | 🟡 Lite | Deterministic evidence-backed artifacts, v4 API. Tailoring levels, answer bank, structural validator: blueprint phase 6 |
| P5 Application engine | 🟡 Lite | State machine, idempotency, approval gate, audit events, kill switch, v5 API; submission is dry-run only. Queue, lanes, extension, local Playwright runner: blueprint phases 5, 8, 9 |
| P6 Scale | 🟡 Lite | Kill switch. Postgres queue and batch lanes: blueprint phase 5/12 |
| Blueprint phase 0 | ✅ Done | Structured errors, secret-redacting logging, deployment mode + tenant config, local Postgres via Docker, green lint/CI |
| Blueprint phases 1–4 | ✅ Done | DB-backed Career Brain / Evidence Graph, shared opportunities + tenant pipeline + DB queue, high-volume discovery, evidence-backed application preparation (`docs/PROJECT_STATE.md`) |
| Blueprint phase 5 | ✅ Done | Deterministic scheduler: one policy engine with reason codes, attempt-based daily/weekly caps (atomic ledger), company cool-down + blocklist, duplicate/repost handling, priority-ordered idempotent enqueueing, `/api/v1/scheduler`, `/dashboard/scheduler` |
| Blueprint phase 6 | ✅ Done | Execution foundation: canonical execution package from READY preparations, guarded attempt state machine, submission-time re-checks (stale / blocklist / cool-down / duplicates / caps), one-submit idempotency with explicit UNKNOWN + verification, human handoff (CAPTCHA/auth/MFA), executor contract with mock + manual executors, `/api/v1/execution`, `/dashboard/execution`. |
| Blueprint phase 7 | ✅ Done | Local Playwright executor: browser runs on your machine (`python -m app.execution.worker`), discovers real forms, fills only safe prepared answers, uploads only real local files, re-checks every gate before the click, never bypasses CAPTCHA/login/MFA (hands off instead), dry-run by default. Greenhouse / Lever / generic forms; Ashby when it uses standard controls. |
| Blueprint phase 8 | ✅ Done | Local document pipeline: each validated preparation becomes a real PDF (and DOCX) resume / cover letter, rendered deterministically with no AI, stored immutably per version with SHA-256, verified again right before the browser uploads it. |
| Blueprint phase 9 | ✅ Done | Browser extension (`extension/`, Manifest V3, plain JS): in your own browser, on the application pages you open, it claims the queued attempt, fills only the server-mapped truthful answers, attaches the rendered documents, intercepts every submit until the local server's gate passes, reports the page evidence for verification, and hands CAPTCHA / login / MFA / unanswered fields to you. |
| Blueprint phase 8b | ✅ Done | Tier-1 gate set (ordered, versioned, explainable; no priority, no top-N), fit bands as versioned candidate settings with the version recorded on every decision, and one AI Gateway that owns every model call (off by default, deterministic fallback everywhere, tenant-private cache, budgets, AI can never create evidence). |

### What currently works end-to-end
- Discovery (Greenhouse/Lever/Ashby) → normalization → deduplication/versioning → persistence
- Eligibility + fit scoring against the Career Brain → `/api/v3/matches` and the shortlist dashboard
- Deterministic tailored artifacts → approval → application state machine (dry-run submission)

### What remains
- Full Tier 1 gate set + fit bands as settings + AI gateway (phase 8b, done), verification + Signal Inbox + outcome attribution (phase 10, done: `app/signals`, `/api/v1/signals`, `/dashboard/signals`), volume-neutral learning engine (phase 11, done: `app/learning`, `/api/v1/learning`, `/dashboard/learning` — observed rates with sample sizes, versioned snapshots, opt-in ordering signal, no policy mutation), high-volume hardening (phase 12, done: concurrency races, execution safety at the final gate, startup recovery, security regression group, diagnostics at `/api/v1/ops/diagnostics` and `/dashboard/ops`, `docs/OPERATIONS.md`), real-world validation (phase 13, done: `tools/validation/phase13_validate.py` runs discovery, mapping, document checks and the pre-submit gate against real public Greenhouse / Lever / Ashby pages in dry run — nothing submitted — and `docs/validation/phase13/REPORT.md` is the compatibility matrix; the fixes it forced: Lever apply URLs, moved apply links, client-rendered and iframe-embedded forms, listing pages never treated as forms, inline CAPTCHA widgets cleared only by a person, upload controls mapped by name, attribution titles folded)

## 3) Phase 1 — Foundation (Completed)

- **Core backend architecture:** FastAPI application with modular route structure (`app/api/routes`).
- **Typed domain modeling:** Pydantic models for profile, skills, projects, experience, achievements, preferences, facts/claims.
- **Truth layer:** `TruthValidator` enforces verification rules before facts are considered application-safe.
- **Career service layer:** `CareerBrainService` loads structured seed data and provides programmatic access/search/relevance helpers.
- **Configuration handling:** `pydantic-settings` based config with `.env` support (`app/config.py`).
- **Testing base:** Unit/API tests for models, validators, service, and v1 endpoints.

Why it matters: this phase establishes reliable, typed, auditable input data for later automation phases.

## 4) Phase 2 — Core Career/Job Intelligence (Completed)

- **Job source adapters:** Greenhouse, Lever, Ashby public API adapters (`app/jobs/sources`).
- **Ingestion pipeline orchestration:** `JobDiscoveryService` coordinates discover → normalize → enrich → deduplicate → persist.
- **Extraction + normalization:** deterministic parsing for job signals; canonical keys and content hashes.
- **Deduplication + provenance:** multi-level identity resolution, cross-source tracking, and job version history.
- **Persistence + schema evolution:** SQLAlchemy ORM + Alembic migrations for jobs/discovery tables.
- **Operational surfaces:** `/api/v2` endpoints and server-rendered dashboard (`/dashboard/*`).
- **Optional enrichment components:** Firecrawl fetcher and LLM fallback provider abstractions are implemented.

Engineering approach: deterministic-first ingestion with explicit provenance and versioning to keep data quality high.

## 5) Phase 3 — Autonomous Application System (In Progress)

### Implemented so far
- **Intelligence domain models/services:** eligibility, requirement interpretation, evidence resolution, scoring, explanation generation, orchestrator (`app/intelligence/services`).
- **Phase 3 database schema:** match policies/runs/job matches/requirement assessments migration (`b11ea16cb71f`).
- **Initial API surface:** `/api/v3/matches` routes are present.
- **Golden-case tests:** intelligence service behavior tested with mocked career context (`tests/intelligence/test_golden_cases.py`).

### Not yet implemented end-to-end
- Autonomous job application execution workflow
- Real browser automation pipeline for submission
- Persisted match recomputation pipeline wired into v3 API (current routes return stub responses)
- Full application state machine/retry orchestration for submission lifecycle

## 6) Architecture

```mermaid
flowchart TD
    User[User Operator] --> API[FastAPI App]

    API --> Phase1[Phase 1 Career Brain APIs]
    API --> Phase2[Phase 2 Job Discovery APIs]
    API --> Phase3[Phase 3 Match APIs Partial]
    API --> Dashboard[Dashboard Views]
    API --> Database[SQLite PostgreSQL via SQLAlchemy]

    Phase2 --> Sources[Source Adapters Greenhouse Lever Ashby]
    Sources --> Normalization[Normalization and Deterministic Extraction]
    Normalization --> LLMFallback[LLM Fallback Optional]
    LLMFallback --> Dedup[Deduplication and Versioning]
    Dedup --> Database

    Phase3 --> MatchServices[Eligibility Requirement Scoring Services]
    MatchServices --> Database
```

## 7) Technology Stack

| Category | Technologies (observed in repo) |
|---|---|
| Backend | Python, FastAPI, Pydantic, pydantic-settings |
| Database | SQLAlchemy, SQLite/PostgreSQL support, Alembic |
| Job Intelligence | Deterministic extraction/normalization, deduplication/versioning |
| AI/LLM | One AI Gateway (`app/ai`): stub / Gemini / local Ollama providers, versioned cache, budgets, schema validation; off by default, never required |
| Web scraping/fetching | httpx, Firecrawl fetcher abstraction |
| Frontend/UI | Jinja2 server-rendered dashboard templates |
| Testing | pytest, pytest-asyncio, FastAPI TestClient, httpx MockTransport |
| Tooling | Ruff, editable package via `pyproject.toml` |

## 8) Current Capabilities

| Capability | Status | Notes |
|---|---|---|
| Career Brain data models and API | ✅ Completed | v1 routes and service layer implemented |
| Job discovery from ATS sources | ✅ Completed | Greenhouse/Lever/Ashby adapters + tests |
| Job normalization and extraction | ✅ Completed | Deterministic extraction + canonicalization |
| Deduplication and provenance tracking | ✅ Completed | Multi-level matching + version snapshots |
| Discovery observability dashboard | ✅ Completed | `/dashboard/`, `/dashboard/jobs`, `/dashboard/runs` |
| Match engine core services | 🟡 In Progress | Services and tests exist; not fully wired to runtime pipeline |
| v3 match API with persisted outputs | 🟡 In Progress | Routes exist but currently stubbed |
| Autonomous application submission | 🔴 Planned | No end-to-end submission executor in repo |
| Browser automation for applications | 🔴 Planned | No Playwright worker pipeline implemented |

## 9) Development Journey

1. **Phase 1:** built a typed, truth-validated career data foundation and stable API surface.  
2. **Phase 2:** introduced production-style ingestion complexity: source adapters, extraction, normalization, deduplication, persistence, and operational dashboarding.  
3. **Phase 3 (current):** started intelligence orchestration (eligibility/fit/explanations) and schema support, but full autonomous application execution is still ahead.

## 10) Future Roadmap

### Near Term
- Wire v3 APIs to real match orchestration and persistence
- Trigger/scaffold match recalculation from discovery events
- Tighten dashboard intelligence views using actual match records

### Medium Term
- Add robust job/application state transitions with retry/recovery
- Expand source integrations and scheduling controls
- Improve observability around run failures and scoring drift

### Long Term
- Production-grade autonomous application execution workers
- Human-in-the-loop controls for approval/override
- Deployment hardening, security, and scale-focused operations

## 11) Running the Project

### Prerequisites
- Python 3.11+
- pip
- Docker (optional, for local PostgreSQL)

### Setup
```bash
cd Range-Apply
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

pip install -e ".[dev]"
cp .env.example .env       # use `copy` on Windows
```

### Environment
Use `.env.example` as the template. Key variables include:
- `DATABASE_URL`
- `CAREER_DATA_PATH`
- `AI_ENABLED` (`false` by default: the whole pipeline runs with zero AI calls), `AI_PROVIDER` (`stub`, `gemini`, `ollama`)
- `FIRECRAWL_API_KEY` (optional)
- `GEMINI_API_KEY` (required only when `AI_PROVIDER=gemini`; `LLM_PROVIDER=gemini` is a legacy alias for the provider choice and never turns AI on)

### Database
Alembic owns the schema; the app refuses to start against an unmigrated
database. Required before first run and after pulling a new migration:
```bash
alembic upgrade head
```
SQLite is the default. For PostgreSQL: `docker compose up -d`, set
`DATABASE_URL=postgresql://careeros:careeros@localhost:5432/careeros`, then
migrate. See `docs/DEVELOPMENT.md`.

### Start backend
```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Access logs are redacted, but `--no-access-log` keeps request lines (which can carry
the dashboard key during login) out of the terminal entirely.

Or, as one local desktop application (loopback only, dry run by default, with
local notifications for READY / attention / submitted / interview / rejection):

```bash
pip install -e ".[desktop]" && alembic upgrade head && python -m app.desktop
python -m app.desktop --install-shortcut   # Windows: a "CareerOS" icon on the Desktop
```

- API docs: `http://localhost:8000/docs`
- Dashboard: `http://localhost:8000/dashboard/`

### Run tests
```bash
pytest
```

### Lint
```bash
ruff check app tests
```

## 12) Project Structure

```text
Range-Apply/
├── app/
│   ├── api/                  # Phase 1 API routes
│   ├── jobs/                 # Phase 2 discovery, extraction, dedup, dashboard
│   ├── intelligence/         # Phase 3 matching models/services (in progress)
│   ├── services/             # CareerBrainService, TruthValidator
│   ├── config.py
│   ├── database.py
│   └── main.py
├── alembic/
│   └── versions/             # DB schema migrations (Phase 2 + Phase 3 tables)
├── docs/
│   ├── jobs/
│   ├── ARCHITECTURE.md
│   ├── DEVELOPMENT.md
│   └── PROJECT_STATE.md
├── tests/
│   └── intelligence/
├── pyproject.toml
└── .env.example
```

## 13) Engineering Highlights

- Clear phase-based modular architecture
- Deterministic-first ingestion and scoring strategy
- Strong separation of domain models vs persistence models
- Explicit migration strategy with Alembic revisions
- Cross-source provenance + version tracking for jobs
- Early match-orchestration scaffolding with testable service boundaries

## 14) Disclaimer / Project Status

This repository is actively developed. PRD phases 1–3 are implemented; phases
4–6 exist in lite form (see §2) and are being extended under
`docs/BLUEPRINT.md`. Nothing in this repository submits a real application
yet: submission is dry-run only until the execution engine (blueprint phase 8)
and browser extension (phase 9) land.

For evaluators: treat autonomous submission and the high-volume queue as
in-progress work, not completed functionality.
