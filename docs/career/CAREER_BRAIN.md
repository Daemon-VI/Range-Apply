# Career Brain

Programmatic interface for accessing Ribhu Siripurapu's structured career information.

**Status:** IMPLEMENTED (Phase 1)  
**Last updated:** August 2026

---

## Purpose

The Career Brain is the foundation for every future CareerOS agent. It answers:

- Who am I?
- What is my current education?
- What roles am I targeting?
- What skills do I actually have?
- Which projects have I built?
- What technologies were used in each project?
- What metrics are verified?
- What experience do I have?
- What achievements do I have?
- What claims can safely be used in a resume?
- What claims can safely be used in an application?
- Which projects are relevant to a particular role?
- Which information requires human verification?

---

## Architecture

```text
Agent / API Client
       ↓
CareerBrainService
       ↓
Structured Career Data (JSON seed)
       ↓
TruthValidator
       ↓
Typed Pydantic Models
```

Markdown files in `docs/career/` and `docs/projects/` are curated human-readable documentation. Agents should use `CareerBrainService`, not raw file reading.

---

## Service Interface

```python
CareerBrainService:
    get_profile() -> Profile
    get_skills() -> list[Skill]
    get_projects() -> list[Project]
    get_project(project_id) -> Project
    get_experience() -> list[Experience]
    get_achievements() -> list[Achievement]
    get_preferences() -> Preference
    get_verified_facts() -> list[CareerFact]
    get_application_safe_facts() -> list[CareerFact]
    search_projects(query) -> list[Project]
    find_relevant_projects(role_or_jd) -> list[Project]
    get_career_summary() -> CareerSummary
```

---

## Data Models

| Model | Purpose |
|-------|---------|
| Profile | Identity, education, contact, links |
| Skill | Categorized skill with evidence and verification |
| Project | Project details, tech stack, metrics, claims |
| Experience | Work/project experience entries |
| Achievement | Hackathons, awards, certifications, academic |
| Preference | Target roles, locations, work mode |
| CareerFact | Atomic verified claim with source and status |
| Claim | Statement with verification metadata |

---

## Truth Layer

All facts pass through `TruthValidator`:

- Verified facts accepted for resume and application use
- Unverified facts flagged, not application-safe
- Inferred facts cannot become application-safe automatically
- Conflicting facts surfaced
- Unsupported claims rejected

---

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| GET /health | Health check |
| GET /profile | Profile information |
| GET /skills | All skills |
| GET /projects | All projects |
| GET /projects/{project_id} | Single project |
| GET /experience | Experience entries |
| GET /achievements | Achievements |
| GET /preferences | Career preferences |
| GET /career-brain | Full career summary |

---

## Future Enhancements (Not Phase 1)

- PostgreSQL persistence with pgvector for semantic retrieval
- Real-time profile updates via admin API
- JD-to-project relevance scoring with embeddings
- Fact verification workflow with user approval UI
