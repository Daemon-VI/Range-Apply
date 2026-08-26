# Job Schema & Models

**Phase 2 Domain Models vs. Persistence Models**

---

## 1. Domain Models (`app/jobs/models/`)

### `RawJob`
Preserves source evidence before normalization:
- `source`: `JobSourceType`
- `source_job_id`: string
- `source_url`: string
- `discovered_url`: string
- `raw_title`: string
- `raw_content`: string (full HTML/Markdown/JSON)
- `content_type`: "html" | "markdown" | "plain" | "json"
- `raw_location`: string
- `raw_metadata`: dict
- `discovered_at` / `retrieved_at`: datetime
- `extraction_method`: string

### `NormalizedJob`
Canonical representation consumed by Phase 3:
- `canonical_key`: deterministic hash (`company|title|location`)
- `company`: string
- `title` / `original_title`: string
- `description` / `original_description`: string
- `location` / `locations`: string / list
- `remote_type`: `REMOTE` | `HYBRID` | `ON_SITE` | `UNKNOWN`
- `employment_type`: `INTERNSHIP` | `FULL_TIME` | `PART_TIME` | `CONTRACT` | `UNKNOWN`
- `experience_level`: `INTERN` | `ENTRY_LEVEL` | `JUNIOR` | `MID` | `SENIOR` | `UNKNOWN`
- `graduation_requirement`: `GraduationRequirement` (`minimum_year`, `maximum_year`, `exact_years`, `original_text`, `extraction_confidence`)
- `required_skills`: list of strings
- `technologies`: list of strings
- `responsibilities`: list of strings
- `qualifications`: list of strings
- `salary_text`: string
- `application_url`: string
- `source_url`: string
- `content_hash`: SHA-256 (64 hex characters)
- `processing_status`: `DISCOVERED` | `FETCHED` | `EXTRACTED` | `NORMALIZED` | `VALIDATED` | `FAILED`
- `job_status`: `ACTIVE` | `CLOSED` | `EXPIRED` | `UNKNOWN`

---

## 2. Database Schema (`jobs`, `job_source_references`, `job_versions`, `discovery_runs`)

Managed via **Alembic** migrations.
- `jobs`: Stores canonical normalized jobs with unique constraint on `(source, source_job_id)`.
- `job_source_references`: Tracks each source where a canonical job was seen.
- `job_versions`: Records historical snapshots when `content_hash` changes.
- `discovery_runs`: Records execution statistics, durations, and error logs.
