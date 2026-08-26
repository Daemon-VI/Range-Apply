# Ingestion Pipeline & Execution

**Phase 2 Job Ingestion Pipeline**

---

## 1. End-to-End Flow

```text
Manual Trigger (POST /api/v2/discovery/run) or Scheduled Run
                          ↓
      Source Adapter (Greenhouse, Lever, Ashby, etc.)
                          ↓
          RawJob (Full HTML/Plain + Metadata)
                          ↓
    Deterministic Normalizer (Title, Skills, Locations)
                          ↓
  LLM Fallback (Only if ambiguous fields like Remote/Grad remain)
                          ↓
          NormalizedJob (Canonical Key + Content Hash)
                          ↓
       Deduplicator (5-Level Identity Hierarchy)
         ├── If New: INSERT jobs & job_source_references
         ├── If Duplicate: UPDATE last_seen_at
         └── If Changed: UPDATE jobs & INSERT job_versions
                          ↓
      DiscoveryRun Record (Statistics & Durations Saved)
```

---

## 2. API Endpoints

| Method | Route | Description |
|---|---|---|
| `GET` | `/api/v2/jobs` | Paginated, filterable list of normalized jobs |
| `GET` | `/api/v2/jobs/{id}` | Detailed job with source references and versions |
| `GET` | `/api/v2/jobs/stats/summary` | Aggregate counts and distributions |
| `GET` | `/api/v2/discovery/runs` | Execution history of discovery runs |
| `POST` | `/api/v2/discovery/run` | Manually trigger discovery for a board token / slug |

---

## 3. Internal Dashboard

Accessible at `http://localhost:8000/dashboard/`:
- `/dashboard/`: Overview metrics, recent jobs, recent runs
- `/dashboard/jobs`: Filterable job search table
- `/dashboard/jobs/{id}`: Detailed job inspection view
- `/dashboard/runs`: History of discovery runs
