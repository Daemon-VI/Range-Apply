# Job Discovery Architecture

**Phase 2 — Job Discovery, Web Intelligence & Normalization**

---

## 1. Overview

Phase 2 turns public job postings into clean, strongly typed, normalized `Job` records.

```text
JOB SOURCES (Greenhouse / Lever / Ashby / Company Pages)
       ↓
DISCOVERY
       ↓
RAW JOB (Preserves original HTML/Markdown/JSON & metadata)
       ↓
DETERMINISTIC EXTRACTION
       ↓
LLM FALLBACK (Only for unresolved ambiguous fields)
       ↓
NORMALIZATION (Canonical key & content hash computation)
       ↓
DEDUPLICATION (5-level deterministic identity resolution)
       ↓
PERSISTENCE (PostgreSQL / SQLite with Alembic migrations)
       ↓
READY FOR PHASE 3 (Career Brain matching & JD intelligence)
```

---

## 2. Ingestion Principles

1. **Permitted, Zero-Cost Access First**: Structured public JSON APIs (Greenhouse, Lever, Ashby) are unauthenticated and free.
2. **Quota Optimization**: Firecrawl is reserved for unstructured public career pages, saving compute and API quotas.
3. **Deterministic First, LLM Second**: Regular expressions, structured metadata, and pattern matching handle standard fields. LLMs only resolve ambiguous fields.
4. **Full Traceability**: `RawJob` preserves original HTML/plain description and source metadata for future inspection.
5. **Multi-Source Provenance**: A canonical job seen across multiple sources retains distinct `SourceReference` records.
