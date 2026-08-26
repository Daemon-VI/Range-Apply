# CareerOS

## Status

In Development — Phase 1 (Career Brain foundation) being implemented.

## Summary

Autonomous Career Operating System that discovers jobs, analyzes JDs, tailors resumes, prepares applications, and tracks every application — evolving from an intelligent career platform into a full autonomous system.

## Problem

Job searching and application is repetitive, time-consuming, and requires maintaining accurate career information across many applications while preserving factual accuracy.

## Solution

Multi-phase autonomous system with Career Brain as the foundation, eventually supporting job discovery, JD analysis, resume tailoring, browser-based application submission, and real-time tracking.

## Architecture (Target — Full System)

```text
Job Sources → Firecrawl → Normalization → Redis Queue
    ↓
JD Analysis → Eligibility → Career Brain → Match Score
    ↓
Resume Tailoring → Application Queue → Playwright Workers
    ↓
Submission → PostgreSQL → Google Sheets → Dashboard + Telegram
```

## Architecture (Phase 1 — Implemented)

```text
FastAPI → CareerBrainService → Structured Career Data → TruthValidator
```

## Technologies

### Phase 1 (Implemented)
Python, FastAPI, Pydantic, SQLAlchemy, pytest

### Planned
PostgreSQL, Redis, Firecrawl, Playwright, Next.js, pgvector, Google Sheets, Telegram, cloud deployment

## My Contribution

Lead developer — architecture, Career Brain design, data models, API, truth system.

## Features

### Phase 1
- Structured career data models
- CareerBrainService programmatic interface
- Truth/verification layer
- REST API for career information
- Comprehensive career context documentation

### Future Phases
- Job discovery and crawling (Firecrawl)
- JD analysis and matching
- Resume tailoring
- Browser automation (Playwright)
- Application tracking (PostgreSQL + Google Sheets)
- Real-time notifications (Telegram)
- 24/7 cloud deployment

## Verified Metrics

N/A for Phase 1 foundation.

## Technical Concepts

- Agentic system architecture
- Structured career data management
- Truth/verification systems
- Typed API design (Pydantic)
- Separation of intelligence, execution, state, and monitoring
- Test-driven development

## Relevant Roles

- Software Engineer / SDE
- Backend Engineer
- AI/ML Engineer
- Product Engineer
- Full Stack Engineer

## Resume-Worthy Facts

- Designing and building autonomous career operating system
- Implemented Career Brain with typed models and truth validation
- Architected multi-phase system separating intelligence, execution, and state
- Built FastAPI service with comprehensive test coverage

## Verified Claims

- Project concept and evolution
- Phase 1 scope and implementation
- Planned technology stack
- Architectural principles

## Unverified Claims

- Future phase completion timelines
- Production deployment status

## Source References

- RIBHU_CAREER_CONTEXT.md — Sections 15, 24, 25, 26, 27
