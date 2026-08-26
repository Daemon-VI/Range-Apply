# Deduplication & Identity Resolution

**Phase 2 Deterministic Identity & Provenance System**

---

## 1. 5-Level Identity Hierarchy

Deduplication follows a strict deterministic hierarchy to prevent duplicate job creation across boards, sources, and runs:

1. **Exact Source Match**: `(source, source_job_id)` in `jobs` or `job_source_references`.
2. **Canonical Application URL**: Direct application URL match after stripping tracking parameters (`utm_*`, fragments).
3. **Canonical Source URL**: Source posting URL match.
4. **Canonical Key**: Deterministic SHA-256 hash of `lowercase(company) | lowercase(normalized_title) | lowercase(location)`.
5. **Location-Aware Content Hash**: SHA-256 hash of canonicalized content matching for the same company and location.

---

## 2. Location Preservation Rule

> **Critical Invariant**: Same Company + Same Title at **different locations** (e.g. `SDE Intern — Hyderabad` vs. `SDE Intern — San Francisco`) are **never collapsed into one job**. They remain distinct canonical jobs.

---

## 3. Cross-Source Provenance & Versioning

- **Cross-Source Discovery**: If a job is discovered on Greenhouse and later on Ashby/LinkedIn, both records are linked to the same canonical `JobRow`, with distinct `SourceReferenceRow` entries created to preserve full multi-source provenance.
- **Meaningful Change Versioning**: If a JD's `content_hash` changes (e.g. deadline or requirements added), the canonical job is updated and a snapshot is recorded in `job_versions`.
- **Idempotency**: Running discovery against the same source multiple times produces 0 new jobs, 0 duplicates in storage, and updates `last_seen_at`.
