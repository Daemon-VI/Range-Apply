# Implementation Context

**Purpose of this file:** a focused map of the codebase so future work does not
need to scan the whole repository. It describes what the code *actually does*
today. For phase status and known gaps see `docs/PROJECT_STATE.md`; for the
system diagram see `docs/ARCHITECTURE.md`; for setup see `docs/DEVELOPMENT.md`.

---

## 1. One-screen module map

```
app/
├── main.py                    FastAPI app, lifespan (verify_schema, reconcile_stale_runs),
│                               exception handlers, router wiring.
├── config.py                  Settings (pydantic-settings), single `settings` singleton.
├── database.py                Engine/session factory, verify_schema()/SchemaNotReadyError
│                               (Alembic owns the schema — see INVARIANTS).
├── security.py                API_KEY check (X-API-Key header for writes,
│                               ?key=/cookie for the dashboard).
│
├── core/
│   └── timeutils.py            utc_now/db_now/to_db/from_db/parse_timestamp — the only
│                                sanctioned boundary between aware-UTC domain models and
│                                naive-UTC database columns.
│
├── models/                    Phase 1 Pydantic domain models: Profile, Skill, Project,
│                               Experience, Achievement, Preference, CareerFact, Claim, enums.
├── services/
│   ├── career_brain.py          CareerBrainService — loads data/career_seed.json,
│   │                             the single source of truth for candidate facts.
│   └── truth_validator.py       Enforces verification_status on career facts.
├── api/routes/                 Phase 1 REST routes (profile, skills, projects, experience,
│                                achievements, preferences, career_brain, health).
├── career/dashboard/           Server-rendered profile-editing dashboard (Jinja2).
│
├── jobs/                       Phase 2 — discovery, normalization, dedup, ingestion.
│   ├── sources/                 JobSource ABC (base.py: fetch_json with retry/backoff/
│   │                             rate-limit) + GreenhouseSource, LeverSource, AshbySource.
│   ├── fetchers/                ContentFetcher ABC + HttpFetcher, FirecrawlFetcher
│   │                             (public-web fallback, never required for the ATS path).
│   ├── extraction/               deterministic.py (regex/section extraction: skills,
│   │                             employment type, remote type, salary, graduation year);
│   │                             llm_provider.py (LLMProvider ABC, StubProvider,
│   │                             GeminiProvider, CachingLLMProvider); llm_fallback.py
│   │                             (LLMFallbackExtractor — only called for fields
│   │                             deterministic extraction left UNKNOWN).
│   ├── normalization/            normalizer.py (JobNormalizer: RawJob -> NormalizedJob,
│   │                             content hash, canonical key); urls.py (normalize_url,
│   │                             urls_equal, escape_like).
│   ├── deduplication/            deduplicator.py (JobDeduplicator — 5-level exact-match
│   │                             identity hierarchy, see INVARIANTS).
│   ├── pipeline/                 discovery_service.py (JobDiscoveryService — orchestrates
│   │                             one run end to end); closure.py (should_sweep,
│   │                             close_missing_jobs — stale-job sweep); run_recovery.py
│   │                             (reconcile_stale_runs — crash recovery for stuck runs).
│   ├── ratelimit.py              TokenBucket / RateLimiterRegistry (source_rate_limiter),
│   │                             per-source, in-process.
│   ├── models/                   RawJob, NormalizedJob, GraduationRequirement,
│   │                             SourceReference, DiscoveryRun, enums.
│   ├── database/models.py        SQLAlchemy rows: JobRow, SourceReferenceRow,
│   │                             JobVersionRow, DiscoveryRunRow.
│   ├── api/routes.py             /api/v2/* — jobs, discovery runs, discovery triggers.
│   └── dashboard/views.py        /dashboard/* — jobs, runs, and the match shortlist UI.
│
├── intelligence/                Phase 3 — requirement extraction, eligibility, matching.
│   ├── adapters/job_adapter.py   job_row_to_normalized(JobRow) -> NormalizedJob. Pure field
│   │                             mapping, no re-extraction — the bridge that makes Phase 3
│   │                             runnable against real discovered jobs, not just fixtures.
│   ├── taxonomy/skills.py        Curated local skill taxonomy + TaxonomyProvider seam
│   │                             (find_skills_in_text, canonicalize, related_skills).
│   ├── extraction/
│   │   └── requirement_extractor.py   RequirementExtractor.extract() — section-aware
│   │                             (split_sections) typed requirement extraction, entirely
│   │                             deterministic, no LLM call.
│   ├── matching/skill_matcher.py SkillMatcher — 4 deterministic layers (exact, alias,
│   │                             adjacent, lexical) + optional layer 5 (SemanticMatcher,
│   │                             CPU-only, off by default).
│   ├── services/
│   │   ├── eligibility_engine.py    EligibilityEngine.evaluate_detailed() — hard gates
│   │   │                            (graduation year, work authorization, location),
│   │   │                            preserves UNCERTAIN.
│   │   ├── evidence_resolver.py     EvidenceResolver — resolves a requirement against
│   │   │                            the Career Brain's skills/projects/experience.
│   │   ├── preference_evaluator.py  Soft ranking signals (role, location, comp
│   │   │                            preferences) — never blocks eligibility.
│   │   ├── fit_scoring_engine.py    FitScoringEngine — policy-weighted component
│   │   │                            scoring (POLICY_VERSION, ENGINE_VERSION,
│   │   │                            DEFAULT_POLICY_WEIGHTS).
│   │   ├── explanation_generator.py ExplanationGenerator — turns scoring output into
│   │   │                            human-readable strengths/gaps/explanation text.
│   │   ├── match_orchestrator.py    MatchOrchestrator.evaluate_job() — wires the above
│   │   │                            into one JobMatch per job (see §2).
│   │   ├── match_persistence.py     run_matching()/latest_run()/latest_matches_query()/
│   │   │                            stale_job_ids() — persists JobMatch to the DB.
│   │   ├── requirement_interpreter.py  Legacy; not wired into the active orchestrator.
│   │   └── factory.py               build_orchestrator() — the one place the Phase 3
│   │                                object graph is assembled.
│   ├── models/                   JobMatch, EligibilityResult, RequirementAssessment,
│   │                             StructuredRequirement, enums (EligibilityStatus,
│   │                             MatchStatus, EvidenceStrength, ConfidenceLevel, ...).
│   ├── database/models.py        MatchPolicyRow, MatchRunRow, JobMatchRow,
│   │                             RequirementAssessmentRow.
│   └── api/routes.py             /api/v3/matches/* — list, detail, recalculate, runs.
│
alembic/versions/               3 migrations: create_jobs_tables, identity_dates_and_
                                 match_provenance, create_phase3_tables.
```

---

## 2. Canonical data flow

**Ingestion (Phase 2), per job, inside `JobDiscoveryService.run_discovery`:**

```
adapter.discover(identifier)                 # GreenhouseSource/LeverSource/AshbySource
  -> List[RawJob]
JobNormalizer.normalize(raw_job, company_name)
  -> NormalizedJob                            # deterministic extraction runs here
LLMFallbackExtractor.enrich_if_needed(job, raw_job)
  -> NormalizedJob                            # only called if a field is still UNKNOWN
JobDeduplicator.process(db, normalized_job, commit=False)
  -> DeduplicationResult(status, job_row, matched_by)
  -> JobRow                                   # persisted inside a SAVEPOINT per job
```

After the run: `should_sweep()` decides whether `close_missing_jobs()` runs
(scoped to `source, source_identifier`).

**Matching (Phase 3), per job, inside `run_matching` -> `MatchOrchestrator.evaluate_job`:**

```
JobRow
  -> job_row_to_normalized(row)               # app/intelligence/adapters/job_adapter.py
  -> NormalizedJob
RequirementExtractor.extract(job)
  -> List[StructuredRequirement]
EligibilityEngine.evaluate_detailed(job)       # hard gates, independent of requirements
  -> EligibilityEvaluation
EvidenceResolver.resolve_skill(...) / total_experience_months() / ...
  -> ResolvedEvidence per requirement          # assembled into RequirementAssessment
PreferenceEvaluator.evaluate(job, preferences)
  -> PreferenceAssessment
FitScoringEngine.score(assessments, eligibility, preferences, job_context)
  -> ScoreResult(fit_score, priority, match_type, confidence, component_scores, ...)
ExplanationGenerator.generate(...)
  -> explanation text
  -> JobMatch                                  # returned by MatchOrchestrator.evaluate_job
_persist_match(db, run_id, job_row, match)
  -> JobMatchRow + RequirementAssessmentRow[]   # app/intelligence/services/match_persistence.py
```

`run_matching` scores jobs inside the same commit-batched-SAVEPOINT shape as
discovery (see `MATCH_COMMIT_BATCH`), so one bad job cannot poison a run.

---

## 3. Invariants a future change must not break

- **Alembic owns the schema.** The app never calls `create_all()` outside
  tests (`app/database.py:create_all_tables` is a test helper only). Startup
  calls `verify_schema()` and fails fast with `SchemaNotReadyError` instead —
  because `create_all()` at boot once left a database populated without an
  `alembic_version` stamp, permanently breaking `alembic upgrade head`.
- **Identity is exact-equality only, never prefix/LIKE.** `JobDeduplicator`'s
  5-level hierarchy compares `normalize_url()` output with `==`. A `LIKE
  '<url>%'` match once merged `/jobs/123` into `/jobs/1234`.
- **The deduplicator never fuzzy-merges.** When no level matches, the job is
  inserted as new. A false merge silently discards a distinct posting; a false
  split just leaves two rows, which is always the safer failure.
- **UNCERTAIN is a real state and must never be rounded to ELIGIBLE.**
  `EligibilityStatus.UNCERTAIN` means the JD did not say, not that the
  candidate qualifies. Collapsing it into ELIGIBLE was the exact bug
  `EligibilityEngine`'s docstring calls out as fixed.
- **Confidence is derived, never hardcoded.** `ConfidenceLevel` on a gate,
  assessment or match is computed from what was actually known (e.g.
  `_assessment_confidence` combines requirement-read confidence with
  match-method confidence) — never a constant `HIGH`.
- **Dates are never fabricated.** `parse_timestamp()` and the adapters return
  `None` when a source does not supply a date; `None` propagates through
  `NormalizedJob.posted_at` / `source_updated_at` / `deadline` rather than
  being defaulted to "now" or dropped silently.
- **The stale-job sweep is scoped to `(source, source_identifier)` and only
  runs after a sufficiently successful run.** `close_missing_jobs` filters on
  both; `should_sweep` refuses when the run didn't complete, discovered zero
  candidates, or failed more than `CLOSURE_MAX_FAILURE_RATIO` of its jobs —
  so a network blip can never close an entire board.
- **The Career Brain is the single source of truth.** `EvidenceResolver`,
  `EligibilityEngine` and `PreferenceEvaluator` only read
  `CareerBrainService`; nothing in Phase 3 invents or infers a capability the
  candidate has not recorded in `data/career_seed.json`.
- **Deterministic processing runs before any LLM call.** Both
  `LLMFallbackExtractor.enrich_if_needed` (Phase 2) and
  `RequirementExtractor` (Phase 3, which never calls an LLM at all) exist so
  that most jobs are processed with zero API calls; an LLM is consulted only
  for fields deterministic extraction left `UNKNOWN`.
- **Domain models are timezone-aware UTC; the database stores naive UTC.**
  `to_db()`/`from_db()` in `app/core/timeutils.py` are the only sanctioned
  conversion points — see that file's module docstring for why (SQLite has no
  native timezone type).

---

## 4. Extension points

- **Add a job source:** subclass `app.jobs.sources.base.JobSource` (implement
  `source_type` and `discover()`; `fetch_json()` gives you retry/backoff/rate
  limiting for free), then register it —
  `JobDiscoveryService.register_source(source_type, source_cls)` or add it to
  the `_sources` dict built in `JobDiscoveryService.__init__`.
- **Add an LLM provider:** subclass `app.jobs.extraction.llm_provider.LLMProvider`
  and implement `extract_ambiguous_fields()`; wire it into
  `get_llm_provider()`. Wrap it in `CachingLLMProvider` to get the on-disk
  content-hash cache for free.
- **Swap the skill taxonomy:** implement `app.intelligence.taxonomy.skills.TaxonomyProvider`
  (`canonicalize`, `find_in_text`, `related`, `category`, `all_skills`) and
  pass it to `SkillMatcher(taxonomy=...)`. See §6 for why the current
  taxonomy is a curated local table rather than ESCO/O*NET/Lightcast.
- **Change scoring:** adjust `FitScoringEngine`'s policy weights (passed via
  `build_orchestrator(policy_weights=...)` or `DEFAULT_POLICY_WEIGHTS` in
  `fit_scoring_engine.py`) and **bump `POLICY_VERSION`** — stored matches
  record the policy version they were scored under
  (`MatchPolicyRow`/`JobMatchRow.policy_version`), so a version bump is what
  makes an old score distinguishable from a new one instead of silently
  reinterpreting it.

---

## 5. Where to look when X breaks

| Symptom | Look here |
|---|---|
| App won't start: `SchemaNotReadyError` | Run `alembic upgrade head`. See `app/database.py:verify_schema`. |
| Write endpoint / dashboard returns 401/503 | `API_KEY` not set outside development. `app/security.py`. |
| Two postings for the same job show up as separate rows | `app/jobs/deduplication/deduplicator.py` — check which of the 5 levels *should* have matched; check `app/jobs/normalization/urls.py:normalize_url` for URL-shape drift. |
| A discovery run is stuck at `running` | Should self-heal via `reconcile_stale_runs` on next run/startup after `STALE_RUN_TIMEOUT_MINUTES`. `app/jobs/pipeline/run_recovery.py`. |
| A board's dead postings stay ACTIVE forever | Check `should_sweep`'s reason in the run's `errors`/logs — `app/jobs/pipeline/closure.py`. Likely the failure ratio or zero-candidates guard tripped. |
| `/api/v2/discovery/run` times out or ties up a worker | It runs in the background by default; only `?wait=true` blocks. `app/jobs/api/routes.py`. |
| A job never gets a match / `/api/v3/matches` is empty for it | No match run has scored it yet — check `/api/v3/matches/runs`, or trigger `/api/v3/matches/recalculate`. Also confirm `job_row_to_normalized` isn't silently defaulting an enum (`app/intelligence/adapters/job_adapter.py:_enum`). |
| A candidate who should qualify shows INELIGIBLE / UNCERTAIN | `app/intelligence/services/eligibility_engine.py` — check each gate's reason string; UNCERTAIN is expected when the JD is silent, not a bug. |
| Fit score looks wrong for a specific requirement | `app/intelligence/services/match_orchestrator.py:_assess*` for how that requirement category is scored, then `app/intelligence/services/fit_scoring_engine.py` for how it's weighted. Full audit trail is `RequirementAssessmentRow` via `/api/v3/matches/{job_id}`. |
| A skill isn't recognized / a false positive skill appears | `app/intelligence/taxonomy/skills.py` — check for a missing alias or a missing disambiguation `context` pattern (see the module docstring on `\bgo\b`/`\brest\b`/`\bspring\b`). |
| Outbound requests to an ATS API are getting throttled | `app/jobs/ratelimit.py` (`SOURCE_RATE_LIMIT_PER_MINUTE`) and `app/jobs/sources/base.py:fetch_json` (retry/backoff, honors `Retry-After`). |
| Timestamps look off by a timezone | You bypassed `to_db()`/`from_db()`. `app/core/timeutils.py`. |
| Test DB / migrations behave differently than production | They shouldn't — `tests/conftest.py` runs `alembic upgrade head` against a real per-process SQLite file, the same command production runs. |

---

## 6. Free-tier architecture

Standing cost rules for this project, and why:

- **No Redis / task queue.** `app/jobs/ratelimit.py` and the pipeline's
  transaction shape are deliberately in-process. Deferred to a later scaling
  phase (P6), and only once a measured workload actually justifies the
  operational cost of a queue — not before.
- **No paid vector DB.** Skill matching is deterministic (exact / alias /
  adjacency / lexical) first; optional semantic similarity runs CPU-only via
  `sentence-transformers`, in-process, with no external service.
- **No hosted cache.** The LLM extraction cache
  (`app.jobs.extraction.llm_provider.CachingLLMProvider`) is a plain
  content-hash-keyed directory of JSON files under `.cache/llm/` — works on
  any host with a writable disk, no Redis/Memcached dependency.
- **Semantic matching is optional and CPU-only**, behind the `semantic` extra
  (`pip install -e ".[semantic]"`) and `SEMANTIC_MATCHING_ENABLED=true`. With
  it off (the default), `SkillMatcher` never imports `sentence-transformers`
  at all. See `app/intelligence/matching/skill_matcher.py`'s module docstring
  for why it's opt-in rather than default: the deterministic layers already
  solve the alias problem that motivated it, at zero install cost.
- **Firecrawl is selective, never required for the ATS path, and never used
  for submission.** Greenhouse/Lever/Ashby are free structured JSON APIs;
  Firecrawl (`app/jobs/fetchers/firecrawl_fetcher.py`) only covers postings
  outside those three.
- **LLM provider abstraction** (`app.jobs.extraction.llm_provider.LLMProvider`)
  so a free tier (Google AI Studio today; Groq, OpenRouter, etc. tomorrow)
  can be swapped without touching `LLMFallbackExtractor` or anything upstream
  of it.
- **Deterministic-first.** `LLMFallbackExtractor` only calls a provider for
  fields deterministic extraction left `UNKNOWN`; `RequirementExtractor`
  (Phase 3) never calls an LLM at all. Most jobs cost zero API calls end to
  end.

**Taxonomy decision.** ESCO, O*NET and Lightcast Open Skills were evaluated
for the skill taxonomy. ESCO and O*NET are occupation-centric classification
systems with weak coverage of the concrete engineering tokens this project
actually needs to match ("gRPC", "React", "Kubernetes" as opposed to
occupation titles); Lightcast Open Skills requires a hosted API key, which
conflicts with the free-tier, offline-capable posture above. A curated local
taxonomy (`app/intelligence/taxonomy/skills.py`) was chosen instead — zero
dependencies, zero network calls, and disambiguation context patterns tuned
to this project's actual false-positive cases (`go`, `rest`, `spring`, …).
`TaxonomyProvider` is the seam left to swap in a hosted taxonomy later if
coverage gaps show up in practice (see §4).
