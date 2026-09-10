# Development Guide

**Last updated:** September 2026

---

## Prerequisites

- Python 3.11+
- pip
- (Recommended) virtual environment

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
| `GEMINI_MODEL` | `gemini-2.0-flash` | Deliberately configurable: Google retires model aliases on its own schedule, and pinning one in code would break silently later. |
| `STALE_RUN_TIMEOUT_MINUTES` | `60` | How long a discovery run can sit at `running` before it's reconciled as `interrupted` on next boot/run. |
| `CLOSURE_MAX_FAILURE_RATIO` | `0.25` | Above this failed-job fraction, the stale-job sweep refuses to close anything (observed set considered incomplete). |
| `SEMANTIC_MATCHING_ENABLED` | `false` | Off by default; requires the `semantic` extra when turned on. |

See `.env.example` for the full list (discovery concurrency, retry/backoff,
per-source rate limit, request timeout, Firecrawl settings, match policy
version, semantic matching thresholds).

---

## Running the app

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

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

---

## Development Commands

```bash
# Run tests (project venv only — see repo-level agent instructions)
pytest

# Run tests with verbose output
pytest -v

# Lint
ruff check app tests

# Format
ruff format app tests
```

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
│   ├── core/                   # timeutils.py — UTC boundary conversions
│   ├── models/                 # Phase 1 Pydantic domain models
│   ├── services/                # CareerBrainService, TruthValidator
│   ├── api/routes/              # Phase 1 REST routes
│   ├── career/dashboard/        # Profile-editing dashboard
│   ├── jobs/                    # Phase 2: discovery, normalization, dedup, ingestion
│   └── intelligence/            # Phase 3: requirement extraction, eligibility, matching
├── alembic/                    # Migrations (authoritative schema)
├── tests/                      # Test suite
├── docs/
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
