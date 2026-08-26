# Development Guide

**Last updated:** August 2026

---

## Prerequisites

- Python 3.11+
- pip
- (Optional) virtual environment

---

## Installation

```bash
# Clone/navigate to project
cd RangeApply

# Create virtual environment (recommended)
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac

# Install with dev dependencies
pip install -e ".[dev]"
```

---

## Environment Variables

Copy the example environment file:

```bash
copy .env.example .env
```

| Variable | Default | Description |
|----------|---------|-------------|
| APP_NAME | CareerOS | Application name |
| APP_ENV | development | Environment |
| DEBUG | true | Debug mode |
| LOG_LEVEL | INFO | Logging level |
| API_HOST | 0.0.0.0 | API bind host |
| API_PORT | 8000 | API port |
| DATABASE_URL | sqlite:///./careeros.db | Database URL |
| CAREER_DATA_PATH | data/career_seed.json | Career seed data path |

---

## Development Commands

```bash
# Start development server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Run tests
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
RangeApply/
├── RIBHU_CAREER_CONTEXT.md    # Canonical career context
├── data/
│   └── career_seed.json       # Structured career data
├── app/
│   ├── main.py                # FastAPI entry point
│   ├── config.py              # Configuration
│   ├── models/                # Pydantic data models
│   ├── services/              # Business logic
│   └── api/                   # API routes
├── tests/                     # Test suite
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DEVELOPMENT.md
│   ├── PROJECT_STATE.md
│   ├── career/                # Career context pack
│   └── projects/              # Project documentation
├── .env.example
├── pyproject.toml
└── README.md
```

---

## Context File Workflow

1. **High-level context:** Read `RIBHU_CAREER_CONTEXT.md` first
2. **Operational state:** Check `docs/PROJECT_STATE.md`
3. **Task-specific context:** Read relevant files in `docs/career/` or `docs/projects/`
4. **Programmatic access:** Use `CareerBrainService` or API endpoints
5. **Structured data updates:** Edit `data/career_seed.json` and corresponding markdown docs

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
- Test categories: models, truth validation, CareerBrainService, API
- Do not add tests that trivially assert the obvious
- Test real behavior: verification filtering, project search, API responses
