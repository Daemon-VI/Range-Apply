# CareerOS

**Autonomous Career Operating System — Phase 1: Career Brain Foundation**

CareerOS is a production-oriented system for autonomous job and internship application. Phase 1 establishes the **Career Brain** — a reliable foundation that understands your professional profile and exposes it through clean, typed, testable interfaces.

## What Phase 1 Delivers

- Structured career data models (Pydantic)
- CareerBrainService programmatic interface
- Truth/verification layer for factual accuracy
- FastAPI REST API
- Comprehensive career context documentation
- Full test suite

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Copy environment config
copy .env.example .env

# Run tests
pytest

# Start API server
uvicorn app.main:app --reload --port 8000
```

API docs: http://localhost:8000/docs

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check |
| `GET /profile` | Profile information |
| `GET /skills` | All skills |
| `GET /projects` | All projects |
| `GET /projects/{id}` | Single project |
| `GET /experience` | Experience entries |
| `GET /achievements` | Achievements |
| `GET /preferences` | Career preferences |
| `GET /career-brain` | Full career summary |

## Project Structure

```text
RIBHU_CAREER_CONTEXT.md     ← Start here
docs/career/                 ← Career context pack
docs/projects/               ← Project documentation
data/career_seed.json        ← Structured career data
app/                         ← Python application
tests/                       ← Test suite
```

## Context Hierarchy

```text
RIBHU_CAREER_CONTEXT.md → docs/PROJECT_STATE.md → relevant context file → source code
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Development Guide](docs/DEVELOPMENT.md)
- [Project State](docs/PROJECT_STATE.md)
- [Career Brain](docs/career/CAREER_BRAIN.md)
- [Truth Rules](docs/career/TRUTH_RULES.md)

## Phase 1 Scope

Phase 1 implements ONLY the Career Brain foundation. It does NOT include job scraping, browser automation, Firecrawl, Playwright, Google Sheets, Telegram, or cloud deployment. Those belong to later phases.

## License

Private project — Ribhu Siripurapu
