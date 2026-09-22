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
│   ├── timeutils.py            utc_now/db_now/to_db/from_db/parse_timestamp — the only
│   │                            sanctioned boundary between aware-UTC domain models and
│   │                            naive-UTC database columns.
│   ├── errors.py               CareerOSError hierarchy (code, http_status, retryable);
│   │                            mapped to JSON by one handler in main.py.
│   ├── logging.py              configure_logging() + RedactingFilter — secrets never
│   │                            reach a log line (API keys, bearer, cookies, session ids).
│   └── ids.py                  new_id() — the UUID4-string primary key helper.
│
├── models/                    Phase 1 Pydantic domain models: Profile, Skill, Project,
│                               Experience, Achievement, Preference, CareerFact, Claim, enums.
│                               These are now *views* rebuilt from the Evidence Graph.
├── services/
│   ├── career_brain.py          CareerBrainService — the stable read interface. DB mode
│   │                             (default) loads the tenant's graph via app/career and
│   │                             bootstraps from career_seed.json on first use; data_path=
│   │                             keeps the legacy read-only JSON mode for fixtures.
│   └── truth_validator.py       Enforces verification_status on career facts.
├── api/routes/                 Phase 1 REST routes (profile, skills, projects, experience,
│                                achievements, preferences, career_brain, health).
├── api/deps.py                 get_tenant_id (DEFAULT_TENANT_ID today), get_repository,
│                                get_career_brain (per request, over the request session).
│
├── career/                     Blueprint Phase 1 — the tenant-scoped Evidence Graph.
│   ├── models.py                EvidenceKind/Grade/SourceType/Status, RelationType,
│   │                             EvidenceNode(+Create/Update), PositioningVariant,
│   │                             AnswerBankEntry, AuditEvent, ImportReport. grade_for()
│   │                             is the single VerificationStatus -> EvidenceGrade map.
│   ├── database/models.py       TenantRow, CandidateProfileRow, EvidenceNodeRow,
│   │                             EvidenceRelationshipRow, PositioningVariantRow(+Evidence),
│   │                             AnswerBankEntryRow, AuditEventRow.
│   ├── repository.py            EvidenceRepository(db, tenant_id) — every read/write for one
│   │                             tenant; versions + before/after audit on every mutation;
│   │                             truth rules (check_truth_rules); caller commits.
│   ├── read_model.py            build_snapshot(repo) -> GraphSnapshot of legacy domain
│   │                             models; node.key is the domain id (evidence references).
│   ├── importer.py              SeedImporter — idempotent hash-keyed import of the seed,
│   │                             derived nodes + relationships; `python -m app.career.importer`.
│   ├── api/routes.py            /api/v1/career/* — profile, preferences, evidence,
│   │                             relationships, positioning, answers, import, audit.
│   └── dashboard/               /dashboard/profile/ — evidence, variants, answers, audit.
│
├── jobs/                       Phase 2 — discovery, normalization, dedup, ingestion.
│   ├── sources/                 JobSource ABC (base.py: fetch_json with retry/backoff+jitter,
│   │                             rate limit, 403 never retried, SourceError.kind, per-source
│   │                             circuit breaker `source_circuits`, RequestStats) +
│   │                             GreenhouseSource, LeverSource, AshbySource, CaptureSource
│   │                             (capture.py: CapturedJob contract, JSON-LD JobPosting first).
│   ├── freshness.py             classify_freshness(): FRESH/RECENT/AGING/STALE/UNKNOWN.
│   ├── fetchers/                ContentFetcher ABC + HttpFetcher, FirecrawlFetcher
│   │                             (public-web fallback, never required for the ATS path).
│   ├── extraction/               deterministic.py (regex/section extraction: skills,
│   │                             employment type, remote type, salary, graduation year);
│   │                             llm_provider.py (LLMProvider ABC, StubProvider,
│   │                             GatewayLLMProvider — job-side extraction through the
│   │                             AI Gateway, schema ExtractedJobFields); llm_fallback.py
│   │                             (LLMFallbackExtractor — only called for fields
│   │                             deterministic extraction left UNKNOWN; per-run budget).
│   ├── normalization/            normalizer.py (JobNormalizer: RawJob -> NormalizedJob,
│   │                             content hash, canonical key); urls.py (normalize_url,
│   │                             urls_equal, escape_like).
│   ├── deduplication/            deduplicator.py (JobDeduplicator — 5-level exact-match
│   │                             identity hierarchy, see INVARIANTS).
│   ├── pipeline/                 discovery_service.py (JobDiscoveryService — orchestrates
│   │                             one run end to end: validate, raw-hash fast path, resume,
│   │                             counters, sweep, source health, candidate projection);
│   │                             closure.py (should_sweep, close_missing_jobs — stale-job
│   │                             sweep); run_recovery.py (reconcile_stale_runs);
│   │                             source_health.py (SourceHealthRepository — reliability
│   │                             as data + next_poll_at back-off; `due()` for run-due).
│   ├── ratelimit.py              TokenBucket / RateLimiterRegistry (source_rate_limiter),
│   │                             per-source, in-process.
│   ├── models/                   RawJob, NormalizedJob, GraduationRequirement,
│   │                             SourceReference, DiscoveryRun, enums.
│   ├── database/models.py        SQLAlchemy rows: JobRow (+freshness), SourceReferenceRow
│   │                             (+content_hash/fetched_at/parse_status), JobVersionRow,
│   │                             DiscoveryRunRow (+volume/AI counters, failure_kind,
│   │                             resume/checkpoint), SourceHealthRow.
│   ├── api/routes.py             /api/v2/* — jobs (freshness filter), discovery runs,
│   │                             run / run-batch / run-due / runs/{id}/resume, capture,
│   │                             sources (health + register), circuits.
│   └── dashboard/views.py        /dashboard/* — jobs, runs, sources, and the shortlist UI.
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
├── tailoring/                   Phase 4 (lite) — deterministic artifact generation.
│   ├── evidence_selector.py      select_evidence(): matched requirements -> verbatim
│   │                              Career Brain evidence text (never synthesised).
│   ├── generator.py              TailoringEngine: resume bullets, cover letter, answers,
│   │                              all string-formatted from evidence; no LLM.
│   ├── database/models.py        TailoredArtifactRow (job_id, match_id, type, version,
│   │                              content, evidence_refs, approved).
│   └── api/routes.py             /api/v4/tailoring/* — generate, list, approve.
│
├── application/                 PRD Phase 5 + 6-lite — application attempts.
│   ├── engine.py                 ApplicationEngine.get_or_create() keyed by (tenant,
│   │                              opportunity), job id resolved to it; prepare()/submit():
│   │                              kill switch -> idempotency -> explicit approval -> adapter,
│   │                              every transition logged as an ApplicationEventRow.
│   ├── adapters/                 ATSAdapter contract; GreenhousePlaywrightAdapter
│   │                              (dry-run only; live path deliberately stops before submit).
│   ├── killswitch.py             KillSwitchRow + is_paused()/set_paused() (global/per-source).
│   ├── database/models.py        ApplicationRow (unique job_id AND unique tenant+opportunity;
│   │                              attempt_number, lane, tailoring_level, cap_day/cap_week,
│   │                              reserved_at/released_at), ApplicationEventRow.
│   └── api/routes.py             /api/v5/applications/* — prepare, submit, list, killswitch.
│
├── execution/                   Blueprint Phase 6 — execution foundation (no browser automation).
│   ├── models.py                 ExecutorKind, ATSFamily, FieldType, ExecutionOutcome/Status,
│   │                              HandoffReason, ErrorClass, VerificationStatus/Method,
│   │                              PreconditionCode; ExecutionTarget, FormField/FormSnapshot,
│   │                              FieldAnswer, ExecutionPackage (canonical input),
│   │                              ExecutionResult, VerificationResult, read models;
│   │                              sanitize_diagnostics() (no credentials, no HTML blobs).
│   ├── database/models.py        ExecutionRunRow (idempotency_key, submit_invoked, verification,
│   │                              handoff, preconditions), FormSnapshotRow, FormFieldRow.
│   ├── target.py                 target_for(): ATS family from source/URL, method, executor hint.
│   ├── package.py                build_package(): READY preparation + target + bank + profile,
│   │                              never regenerated.
│   ├── forms.py                  map_fields(): files -> artifacts, identity -> profile, exact key
│   │                              -> prepared answer -> bank, category, typed fit (options /
│   │                              numeric / date), NEEDS_USER_INPUT for facts, NEEDS_REVIEW
│   │                              for unknown types; form_fingerprint(); blocking().
│   ├── preconditions.py          PreconditionChecker.check(): attempt/preparation/opening/
│   │                              blocklist/cool-down/duplicates/caps at submission time;
│   │                              reserve=True consumes or refreshes the admission slot.
│   ├── executors/base.py         Executor protocol: can_handle / prepare / execute / verify;
│   │                              ExecutorError(before_submit=...).
│   ├── executors/mock.py         MockExecutor (scripted outcomes, submit_calls counter, no I/O).
│   ├── executors/manual.py       ManualExecutor: always HANDOFF USER_CONFIRMATION_REQUIRED.
│   ├── playwright/               Blueprint Phase 7 — the local browser executor.
│   │   ├── browser.py            BrowserSession (launch once, page per execution, explicit
│   │   │                          timeouts, atexit close, relaunch after a crash; optional
│   │   │                          LOCAL persistent profile), Pacer (min delay between pages).
│   │   ├── discovery.py          scan_page(): one in-page script -> PageScan (fields with
│   │   │                          label/type/required/options/stable selector, CAPTCHA / login /
│   │   │                          MFA / custom-widget detection, submit buttons, errors,
│   │   │                          success marker + reference extraction). No HTML persisted.
│   │   ├── strategies.py         GREENHOUSE / LEVER / ASHBY (strict_controls) / GENERIC: submit
│   │   │                          locators, success URL/text patterns, known field names.
│   │   └── executor.py           PlaywrightExecutor: prepare (navigate, scan, handoff check),
│   │                              execute (fill typed answers, real-file uploads only, rescan,
│   │                              pre-submit gate, DRY_RUN or click, classify the aftermath),
│   │                              verify (page evidence only). Never solves a challenge.
│   ├── executors/extension.py    BrowserExtensionExecutor (Phase 9): can_handle browser-form
│   │                              targets, verify from the evidence the extension reported
│   │                              (reference/text -> VERIFIED, redirect -> LIKELY, else UNKNOWN);
│   │                              prepare/execute refused in-process (the browser is the person's).
│   ├── worker.py                 LocalWorker / `python -m app.execution.worker`: claim SUBMIT
│   │                              items, run the Playwright executor, one browser, clean stop.
│   ├── service.py                ExecutionService: ready/preview/enqueue_ready/claim/start
│   │                              (guarded READY->SUBMITTING mutex)/execute/capture_form/
│   │                              report_result (executor verify when none reported)/verify/
│   │                              confirm/handoff/retry/cancel/answer_field/run_queue/summary;
│   │                              extension: match_for_url/claim_item/heartbeat/gate.
│   ├── api/routes.py             /api/v1/execution/* (preview -> claim -> start -> form ->
│   │                              result | handoff -> verify | confirm; retry, cancel, fields;
│   │                              /extension/{status,match,claim,heartbeat,gate}).
│   └── dashboard/                /dashboard/execution, /dashboard/execution/attempts/{id}.
│
extension/                       Blueprint Phase 9 — the second executor, in the candidate's browser.
├── manifest.json                 MV3; storage/activeTab/scripting/tabs/alarms; localhost hosts only.
├── src/api.js                    Local API client (base URL must be localhost; key from storage).
├── src/background.js             Service worker: match -> claim -> start -> discover -> form ->
│                                  documents -> fill -> arm -> gate -> result | handoff | confirm.
├── src/content.js                Page bridge: discover, fill, submit interception (capture phase,
│                                  released only after the server gate), evidence observation.
├── src/discover.js               THE form-discovery script (also loaded by playwright/discovery.py).
├── src/fill.js                   Typed filling; DataTransfer file attachment; submit lookup.
├── src/popup.*, src/options.*    Per-tab status/actions; server address, API key, submit mode.
└── README.md                     Install (unpacked), modes, guarantees, permissions.
│
├── ai/                          Blueprint Phase 8b — the AI Gateway: every model call, optional.
│   ├── models.py                 AIOperation, AIScope (job | candidate), AIStatus, AIRequest,
│   │                              AIResponse (.metadata(): no prompt/output), TenantAISettings,
│   │                              EffectiveAIConfig, GATEWAY_VERSION.
│   ├── providers.py              AIProvider protocol; StubProvider, ScriptedProvider (tests),
│   │                              GeminiProvider (header auth), OllamaProvider (local); build_provider().
│   ├── cache.py                  cache_key() (gateway/prompt/schema versions, provider/model,
│   │                              normalized input, tenant for candidate scope); AICache (JSON
│   │                              files, corrupt/stale = miss), MemoryCache.
│   ├── budget.py                 CallBudget per discovery run / preparation package.
│   ├── gateway.py                GlobalAIConfig.from_settings(), AIGateway.effective() (global ∧
│   │                              tenant, narrowing only), run(): rules -> cache -> budget ->
│   │                              provider -> validation -> cache -> accounting; UsageSink
│   │                              (counters + ai_usage rows); get_gateway()/reset_gateway().
│   ├── database/models.py        AIUsageRow (metadata only).
│   └── api/routes.py             /api/v1/ai/{config, status, usage, cache/clear}.
│
├── learning/                    Blueprint Phase 11 — the volume-neutral Outcome Learning Engine.
│   ├── models.py                 LEARNING_VERSION / FEATURE_VERSION / SMOOTHING_METHOD /
│   │                              ORDERING_VERSION; Dimension, Metric, LearningConfidence,
│   │                              EvidenceQuality, OutcomeCompleteness; TenantLearningSettings
│   │                              (ordering_enabled=False, window_days, min_samples, prior_strength,
│   │                              minimum_evidence); LearningRow; RateEstimate, GroupMetrics,
│   │                              Recommendation, LearningResult, ExpectedResponse; read models.
│   ├── stats.py                  smoothed_rate() (beta-binomial toward the baseline), wilson_interval(),
│   │                              confidence_for(), estimate(), median(). Pure.
│   ├── dataset.py                build_dataset(db, tenant, as_of, window_days, minimum_evidence):
│   │                              time-aware (events by observed_at <= as_of), evidence-aware rows.
│   ├── engine.py                 LearningEngine: dataset / aggregate (pure) / compute /
│   │                              source_discovery (job side) / recommendations / snapshot (persist,
│   │                              never overwrite) / metrics / expected_response (opt-in ordering
│   │                              signal); LearnedPrior (one index per sync run).
│   ├── database/models.py        LearningSnapshotRow, LearningMetricRow, LearningRecommendationRow.
│   ├── api/routes.py             /api/v1/learning/*.
│   └── dashboard/                /dashboard/learning.
│
├── signals/                     Blueprint Phase 10 — verification, Signal Inbox, outcome attribution.
│   ├── models.py                 SignalSource, SignalCategory, Confidence, ClassificationSource,
│   │                              SignalStatus, AttributionStatus, OutcomeKind, EvidenceStrength,
│   │                              EventOrigin, TimeBasis; SignalIngest / EmailMessage (connector
│   │                              boundary), AttributionHints; Classification, AttributionResult,
│   │                              DerivedOutcome; read models + SignalTrace; the four versions.
│   ├── database/models.py        SignalRow (dedupe_key unique per tenant), SignalObservationRow,
│   │                              SignalAttributionRow (append-only), OutcomeEventRow (append-only,
│   │                              dedupe_key), ApplicationOutcomeRow (derived, versioned).
│   ├── normalize.py              strip_html / normalize_text (quotes, signatures) / redact_credentials
│   │                              / excerpt_of / content_hash / sender_domain / employer_domain (ATS
│   │                              mailers excluded) / extract_references|urls|uuids; email_to_ingest;
│   │                              NormalizedSignal.
│   ├── classify.py               RULES (named regexes), PRECEDENCE, compatible pairs, classify_text()
│   │                              (pure; CLASSIFIER_VERSION).
│   ├── ai.py                     GatewaySignalClassifier (CLASSIFY_SIGNAL, candidate scope, enum-only,
│   │                              capped at MEDIUM), get_signal_classifier().
│   ├── attribution.py            AttemptIndex (one load per batch), Attributor.attribute():
│   │                              strong ids -> thread -> reference -> embedded ids -> URL ->
│   │                              company(+title); MATCHED / AMBIGUOUS / UNMATCHED with evidence.
│   ├── outcomes.py               OUTCOME_FOR_CATEGORY, STAGE, TERMINAL, evidence_for(),
│   │                              outcome_for_verification(), derive() (OUTCOME_RULES_VERSION).
│   ├── execution.py              execution_signal(run, attempt) / emit_execution_signal(): Phase 6/7/9
│   │                              verification -> signal (reference run:verification:method).
│   ├── service.py                SignalInboxService / SignalIngestionService: ingest (idempotent),
│   │                              process (classify -> attribute -> event -> derive -> lifecycle ->
│   │                              verify bridge), review actions (link / confirm / reject / confirm
│   │                              outcome / ignore / merge / reprocess), queries, trace, summary,
│   │                              purge_excerpts; audit on the "signal" / "application_outcome" entities.
│   ├── api/routes.py             /api/v1/signals/*.
│   └── dashboard/                /dashboard/signals, /dashboard/signals/{id}.
│
├── documents/                   Blueprint Phase 8 — local, deterministic PDF/DOCX artifacts.
│   ├── models.py                 DocumentFormat/Status/Validation, Contact, Section,
│   │                              DocumentModel (expected_lines()), RenderInput/Result,
│   │                              ValidationOutcome, DocumentArtifact, RenderReport.
│   ├── database/models.py        DocumentArtifactRow (immutable versions per preparation/type/
│   │                              format: SHA-256, input fingerprint, evidence fingerprint,
│   │                              renderer+version, bytes, pages, relative path, status).
│   ├── storage.py                ArtifactStore: identifier-safe tenant/co/prep paths under
│   │                              documents_root, write-once atomic writes, verify (magic, size,
│   │                              SHA-256, bound); sha256_bytes/sha256_file.
│   ├── content.py                resume_model()/cover_letter_model(): blocks -> DocumentModel
│   │                              verbatim, in order; contact_from_profile(); normalize_text().
│   ├── renderers/base.py         Renderer protocol (name, version, format, can_render, render),
│   │                              RenderError, ascii_safe() typographic fallback.
│   ├── renderers/pdf.py          PdfRenderer (fpdf2): A4, core font or embedded TrueType only
│   │                              when content needs it, links, page numbers, fixed metadata.
│   ├── renderers/docx.py         DocxRenderer (python-docx): same model, hyperlinks, bullets,
│   │                              normalised zip timestamps for byte determinism.
│   ├── validate.py               extract_text() (pypdf / python-docx), validate_document():
│   │                              parses, pages, empty pages, page bound, every expected line.
│   ├── service.py                DocumentService: model_for, fingerprint, get_or_render (reuse /
│   │                              invalidate / new version), ensure_for_execution, materialize
│   │                              (verified path; NEEDS_REVIEW refused for upload), invalidate,
│   │                              regenerate.
│   └── api/routes.py             /api/v1/documents/* (render, ensure, list, detail,
│                                  materialize, file, invalidate, regenerate).
│
├── scheduler/                   Blueprint Phase 5 — deterministic scheduler + policy enforcement.
│   ├── caps.py                   period_keys() (tenant-timezone day / ISO week), CapLedger
│   │                              (conditional UPDATE reserve, release; cap 0 = paused).
│   ├── attempts.py               AttemptRepository: reserve() (slot + attempt in one savepoint),
│   │                              mark_ready()/mark_preparing()/release(); CooldownIndex and
│   │                              duplicate index built from the tenant's attempts.
│   ├── decisions.py              decide(): pure verdict with an AdmissionReason; ordering_key():
│   │                              priority desc, deadline asc, first_seen desc, opportunity id.
│   ├── service.py                SchedulerService: preview() (read-only), run() (reconcile ->
│   │                              decide in order -> admit under caps/window -> optional
│   │                              run_prepare_queue -> reconcile), capacity(), history(),
│   │                              ready_for_execution(); RUNNING lease with heartbeat.
│   ├── database/models.py        SchedulerRunRow, ApplicationCapLedgerRow.
│   ├── api/routes.py             /api/v1/scheduler/{preview,run,status,capacity,runs,queue}.
│   └── dashboard/                /dashboard/scheduler, /dashboard/scheduler/runs/{id}.
│
├── pipeline/                    Blueprint Phase 2 — opportunities, decisions, policy, queue.
│   ├── models.py                 OpportunityState (+ALLOWED_TRANSITIONS), EligibilityDecision
│   │                              (+ELIGIBILITY_RANK), FitBand, Lane, TailoringLevel,
│   │                              QueueAction/QueueState, read models, ApplicationPolicy.
│   ├── database/models.py        SHARED: OpportunityRow, OpportunityJobRow. TENANT:
│   │                              CandidateOpportunityRow, EligibilityDecisionRow,
│   │                              PriorityScoreRow, ApplicationPolicyRow, ApplicationQueueRow.
│   ├── identity.py               identity_for_job(): company | normalised title | location
│   │                              bucket -> exact identity key (coarser than canonical_key).
│   ├── policy.py                 band_for(), fit_band_config(), evaluate_admission() — derives
│   │                              from the Tier-1 gate report; entirely candidate settings;
│   │                              priority is never consulted.
│   ├── gates.py                  Blueprint Phase 8b — the Tier-1 gate set: GATE_RULESET_VERSION,
│   │                              Gate, GATE_CATALOG (ordered; static + stateful, each mapped
│   │                              to an AdmissionReason), evaluate_gates() -> GateReport
│   │                              (every gate reported; first failure decides), catalog().
│   ├── priority.py               compute_priority(): deterministic, versioned, order only.
│   ├── repository.py             OpportunityRepository (resolve_opportunity, candidate state
│   │                              machine, decisions, fit link, priority, admission,
│   │                              company_in_cooldown) and PolicyRepository.
│   ├── queue.py                  QueueRepository — enqueue (idempotent), claim (SKIP LOCKED
│   │                              on Postgres / guarded UPDATE on SQLite), lease, outcomes.
│   ├── sync.py                   sync_match()/sync_run(): job_matches -> candidate
│   │                              opportunities; called after every match run.
│   ├── api/routes.py             /api/v1/opportunities, /api/v1/policy, /api/v1/queue.
│   └── dashboard/                /dashboard/opportunities, /dashboard/queue, /dashboard/policy.
│
├── preparation/                 Blueprint Phase 4 — evidence-backed application packages.
│   ├── models.py                 PreparationStatus, ValidationStatus, CoverLetterMode, Block
│   │                              (FRAMING | CLAIM + evidence_keys), AnswerSource,
│   │                              PreparedAnswerStatus, QueueOutcome, read models.
│   ├── database/models.py        ApplicationPreparationRow (tenant + candidate opportunity +
│   │                              version, fingerprint, inputs), PreparationArtifactRow,
│   │                              PreparationAnswerRow.
│   ├── evidence.py               EvidenceSnapshot (one load per tenant; safe_keys; children;
│   │                              fingerprint) + select_evidence() per level.
│   ├── positioning.py            select_variant() — deterministic, candidate-authored only.
│   ├── composer.py               compose_resume()/compose_cover_letter()/render(): blocks
│   │                              string-built from cited nodes.
│   ├── validator.py              PreparationValidator — TruthValidator.validate_claim per
│   │                              CLAIM + unresolved/removed/unsafe evidence, unsupported
│   │                              number/date/skill; `context` = job facts + framing.
│   ├── questions.py              classify_question(), AnswerResolver (bank → category →
│   │                              profile → evidence template → NEEDS_USER_INPUT/REVIEW).
│   ├── ai.py                     TextPolisher protocol, StubPolisher (default), GatewayPolisher
│   │                              (L2 reword-only requests through the AI Gateway, candidate
│   │                              scope, per-package budget, metadata; never evidence).
│   ├── service.py                PreparationService — prepare()/prepare_many(), fingerprint
│   │                              idempotency, versions, review, answer capture, audit.
│   ├── queue_worker.py           process_prepare_item()/run_prepare_queue().
│   ├── api/routes.py             /api/v1/preparations/*.
│   └── dashboard/                /dashboard/preparations, /dashboard/preparations/{id}.
│
alembic/versions/               14 migrations: create_jobs_tables, identity_dates_and_
                                 match_provenance, create_phase3_tables,
                                 tailoring_and_application_engine, create_career_brain_tables,
                                 opportunities_policy_queue, discovery_volume,
                                 application_preparation, scheduler_runs_and_caps,
                                 execution_foundation, document_artifacts,
                                 phase8b_gates_bands_ai, phase10_signal_inbox, phase11_learning.
```

The target module layout for the remaining blueprint phases is in
`docs/BLUEPRINT.md` §3 and §13.

---

## 2. Canonical data flow

**Ingestion (Phase 2 + Blueprint Phase 3), per job, inside `JobDiscoveryService.run_discovery`:**

```
adapter.discover(identifier, **adapter_kwargs)   # Greenhouse/Lever/Ashby/CaptureSource
  -> List[RawJob]
validate_raw_job(raw)                            # malformed -> counted as rejected, no work
compute_raw_hash(raw)                            # hash of the payload as fetched
  == SourceReferenceRow.content_hash ?           # UNCHANGED FAST PATH: touch last_seen,
                                                 # refresh freshness, resolve opportunity, stop
JobNormalizer.normalize(raw_job, company_name)
  -> NormalizedJob                               # deterministic extraction runs here
LLMFallbackExtractor.enrich_if_needed(job, raw)  # only for UNKNOWN fields, only with a real provider
JobDeduplicator.process(db, job, commit=False)
  -> DeduplicationResult(status, job_row, matched_by, reopened)
  -> JobRow (+ JobVersionRow only if content_hash changed)
SourceReferenceRow.content_hash/fetched_at/parse_status updated
JobRow.freshness = classify_freshness(...)
OpportunityRepository.resolve_opportunity(db, job_row)   # THE only opportunity creation path
  -> (OpportunityRow, created, how)             # how: new | identity_key | existing_link | repost
```

All of that runs inside a SAVEPOINT per job. After the loop: `should_sweep()`
decides whether `close_missing_jobs()` runs (scoped to `source,
source_identifier`); closed jobs become STALE and opportunities whose jobs
are all closed are closed too. `_finalize_run` writes every counter and folds
the run into `source_health`; `_project_candidates` then inserts DISCOVERED
`candidate_opportunities` rows for `DISCOVERY_PROJECT_TENANTS` (no scoring).

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

**Opportunity projection (Blueprint Phase 2), after discovery and after matching:**

```
JobRow (persisted by dedup)
  -> OpportunityRepository.resolve_opportunity(db, job)      # inside the discovery savepoint
  -> OpportunityRow (shared) + OpportunityJobRow             # exact identity key; reposts flagged

JobMatchRow (persisted by run_matching)
  -> sync_match(db, tenant_id, match)                        # app/pipeline/sync.py
     -> CandidateOpportunityRow                              # DISCOVERED -> ELIGIBLE | UNCERTAIN | INELIGIBLE
     -> EligibilityDecisionRow                               # decision + reason codes + ruleset version
     -> fit link (match_id, fit_score, fit_band via policy thresholds)
     -> PriorityScoreRow                                     # compute_priority(), order only
     -> policy_admitted / policy_reason                      # evaluate_admission(), never priority
```

Enqueueing (`QueueRepository.enqueue`) is keyed by `(tenant, opportunity,
action)`; the API refuses it unless the candidate opportunity is admitted
(or `force` is set, audited).

**Preparation (Blueprint Phase 4), per candidate opportunity, inside `PreparationService`:**

```
EvidenceSnapshot.load(tenant)                 # once per service: active nodes, safe_keys,
                                              # relationships, profile, variants, approved answers
targets = jobs + matches + assessments        # IN queries for the whole batch
select_variant(variants, job title, matched keys)
select_evidence(snapshot, assessments, variant, level)   # safe evidence only; excluded recorded
compose_resume() / compose_cover_letter()     # FRAMING (candidate/job text) + CLAIM (evidence keys)
[L2 only] polisher.polish(block) -> validate again -> accept or keep original
PreparationValidator.validate_blocks(blocks, context)   # TruthValidator + structural checks
AnswerResolver.resolve(question)              # bank -> category -> profile -> template -> ask
status: READY | NEEDS_USER_INPUT | NEEDS_REVIEW; fingerprint decides reuse vs new version
CandidateOpportunity -> PREPARED | IN_REVIEW; audit events; nothing is submitted
```

**Scheduling (Blueprint Phase 5), per tenant, inside `SchedulerService.run`:**

```
acquire: no other RUNNING run for the tenant (or its heartbeat is stale / force)
load: policy, every candidate opportunity + opportunity, attempts, PREPARE items,
      latest preparations  -> cool-down index + duplicate index (fixed query count)
reconcile in-flight attempts: PREPARE SUCCEEDED + READY -> attempt READY (+preparation_id);
      closed / skipped / newly blocked / item FAILED or CANCELLED -> release slot,
      cancel item, CandidateOpportunity -> CLOSED where it applies
sort by ordering_key (priority desc, deadline asc, first_seen desc, id)
for each: verdict = decide(ctx, co, opp)            # pure; see decisions.py for the order
   admissible and within window and slot available:
      savepoint { ledger.reserve(day, week) ; attempt QUALIFIED reserved ;
                  enqueue PREPARE (idempotent) | reuse READY package | requeue ;
                  CandidateOpportunity -> QUEUED ; audit "scheduled" }
   admissible beyond the window -> WINDOW_DEFERRED (next run picks it up)
   no slot -> DAILY_CAP_REACHED / WEEKLY_CAP_REACHED for the rest of the run
   every row: scheduler_code / scheduler_reason / scheduler_run_id stamped
commit every 100 admissions with a heartbeat on the run row
[prepare=true] run_prepare_queue(limit) -> reconcile again -> ready_for_execution
run row: COMPLETED | FAILED with counts, blocked_by_reason, samples, errors
READY attempts -> ExecutionService.enqueue_ready(): SUBMIT queue items (idempotent)
```

**Execution (Blueprint Phase 6), per SUBMIT queue item, inside `ExecutionService`:**

```
claim (QueueRepository.claim, action=SUBMIT, lease)           # queue owns scheduling state
start(item, worker, executor):
   attempt for the item's opportunity (tenant-checked)
   SUBMITTED/VERIFIED -> item SUCCEEDED (already submitted)   # idempotency
   UNCERTAIN          -> item NEEDS_REVIEW (verify first)
   SUBMITTING + RUNNING run: submit_invoked -> UNKNOWN, attempt UNCERTAIN
                             else -> old run FAILED_RETRYABLE, attempt READY, continue
   preconditions (reserve=True): attempt READY, preparation READY/current/validated,
      answers complete, opening open, not blocked, no cool-down, no duplicate,
      cap slot consumed (same period) or refreshed (new period) or wait
   guarded UPDATE applications SET status=SUBMITTING, submission_key=<key>
      WHERE status='READY' AND submission_key IS NULL      # the mutex; 0 rows = lost race
   ExecutionRunRow RUNNING (idempotency_key tenant:attempt:attempt_no:run_no)
execute: executor.prepare(package) -> FormSnapshot -> capture_form (structure + mapped answers)
   required NEEDS_USER_INPUT -> attempt NEEDS_USER_INPUT, item BLOCKED
   required NEEDS_REVIEW     -> HANDOFF UNKNOWN_REQUIRED_FIELD
   run.submit_invoked = True; commit                          # before the executor may press submit
   executor.execute(...) -> ExecutionResult | ExecutorError(before_submit?)
report_result:
   SUBMITTED  -> attempt SUBMITTED (+ verify -> VERIFIED / LIKELY / FAILED->NEEDS_REVIEW)
   UNKNOWN    -> attempt UNCERTAIN, run UNKNOWN, item NEEDS_REVIEW; never resubmitted
   RETRYABLE  -> run FAILED_RETRYABLE, attempt READY (slot kept), item RETRY_WAIT
   PERMANENT  -> attempt FAILED (slot released), item FAILED
   HANDOFF    -> attempt BLOCKED (+blocked_reason, where it stopped, what remains), item BLOCKED
   NEEDS_USER_INPUT / NEEDS_REVIEW / FORM_CHANGED -> as named
verify / confirm: the only exits from UNCERTAIN; a person's "not submitted" -> NEEDS_REVIEW
retry / cancel / answer_field: human actions; retry refused while an UNKNOWN run is unverified
```

**Documents (Blueprint Phase 8), inside `DocumentService.get_or_render`:**

```
preparation must be READY (SUPERSEDED / INVALIDATED are refused)
model = blocks -> DocumentModel (+ contact from the profile, job facts, optional date line)
fingerprint = sha256(preparation id+version+fingerprint, model, options, renderer, version, font)
ACTIVE artifact with the same fingerprint and an intact file -> reuse (no write)
   intact = exists, non-empty, size == recorded, magic bytes match format, SHA-256 == recorded
   else -> invalidate it (file kept for history), continue
render -> validate_document(): parses, >0 pages, no empty page, page bound (else NEEDS_REVIEW),
   every model.expected_lines() found in the extracted text (else FAILED -> DocumentError)
write-once to <root>/<tenant>/<co>/<prep>/<type>-vN.<ext> -> DocumentArtifactRow (version N)
audit "rendered" with hashes and counts only, never document text
execution start(): ensure_for_execution -> resume PDF (+ cover letter when enabled);
   NEEDS_REVIEW artifacts are refused for upload; problems -> attempt NEEDS_REVIEW
   paths + {id, version, sha256} into package.execution_config; ids on the ExecutionRunRow
executor upload: re-hash the file right before set_input_files; mismatch -> not uploaded
```

**Local browser execution (Blueprint Phase 7), inside `PlaywrightExecutor`:**

```
prepare:  pacer.wait -> page.goto(target.canonical_url) [nav timeout] -> scan_page
          CAPTCHA / MFA / login wall / no standard controls (Ashby) / no form
             -> remembered as a handoff (execute returns HANDOFF before touching anything)
          -> FormSnapshot(fields with selectors, metadata: strategy, handoff, dry_run)
service:  capture_form -> map_fields (exact question -> prepared/bank -> category; never guess)
          required NEEDS_USER_INPUT -> attempt NEEDS_USER_INPUT; required unknown -> HANDOFF
          run.submit_invoked = True; commit
execute:  fill only ANSWERED fields by type (fill / select_option / check / set_input_files)
          file field: real local file from EXECUTION_RESUME_FILE / _COVER_LETTER_FILE or
             -> HANDOFF ARTIFACT_FILE_REQUIRED (required) / skipped (optional); never a fake file
          rescan (late CAPTCHA?) -> handoff; find submit control -> else HANDOFF AMBIGUOUS_FORM
          dry_run -> DRY_RUN (attempt READY, item NEEDS_REVIEW, nothing pressed)
          gate() -> PreconditionChecker.check(executing=True) + kill switch -> NEEDS_REVIEW if any
          click submit; poll up to PLAYWRIGHT_SUBMIT_WAIT_MS:
             confirmation text / URL -> SUBMITTED (+reference)   validation error -> NEEDS_REVIEW
             CAPTCHA -> HANDOFF (submit_attempted)               page gone / timeout -> UNKNOWN
verify:   reference on page -> VERIFIED(application_id); confirmation text -> VERIFIED;
          confirmation-looking URL only -> LIKELY; nothing -> UNKNOWN
```

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
  candidate has not recorded. The service reads the tenant's Evidence Graph;
  `data/career_seed.json` is the *initial import*, not the runtime store.
- **Evidence references are node keys, scoped to a tenant.** `Skill.id`,
  `Project.id`, `Experience.id`, `CareerFact.id` are `evidence_nodes.key`.
  Downstream artifacts store those keys; resolve them with
  `EvidenceRepository.get_nodes_by_keys` (never assume a key is global).
- **Extraction is not verification.** `EvidenceRepository.create_node`
  refuses to create INFERRED/LLM_EXTRACTED evidence as VERIFIED; a human
  confirmation is an update that records `verified_by`. Only VERIFIED
  (grade CONFIRMED) evidence may be application-safe.
- **Every Career Brain mutation is versioned and audited.** The repository
  bumps `version` and writes an `audit_events` row with before/after
  snapshots; a write path that bypasses the repository breaks that guarantee.
- **Deterministic processing runs before any LLM call.** Both
  `LLMFallbackExtractor.enrich_if_needed` (Phase 2) and
  `RequirementExtractor` (Phase 3, which never calls an LLM at all) exist so
  that most jobs are processed with zero API calls; an LLM is consulted only
  for fields deterministic extraction left `UNKNOWN`.
- **Domain models are timezone-aware UTC; the database stores naive UTC.**
  `to_db()`/`from_db()` in `app/core/timeutils.py` are the only sanctioned
  conversion points — see that file's module docstring for why (SQLite has no
  native timezone type).
- **Errors are structured and never swallowed.** Services raise
  `app.core.errors.CareerOSError` subclasses; `app/main.py` owns the HTTP
  mapping. A `try/except` either re-raises, converts to a structured error,
  or logs with a traceback and records the failure on the run/attempt row.
- **Secrets and document text never reach logs.** `configure_logging()`
  installs `RedactingFilter` on every handler; call sites still must not log
  keys, cookies, tokens, resumes or answers.
- **Volume is never reduced by the system.** Ranking, prioritisation and
  learning decide order and processing strategy. An eligible job in a band
  the candidate enabled always enters the pipeline (`docs/BLUEPRINT.md` §0).
  Concretely: `evaluate_admission` takes eligibility, band and company and
  no priority; `record_eligibility` never moves a candidate opportunity
  that is already QUEUED or beyond; UNCERTAIN ranks above INELIGIBLE.
- **Source identity ≠ opportunity identity.** `JobDeduplicator` merges
  *postings*; `identity_for_job` groups postings into one *opening* by an
  exact normalised key. Neither layer fuzzy-merges. Candidate state hangs
  off the opportunity, so two source rows can never yield two applications
  for one opening (`uq_queue_tenant_opportunity_action`,
  `uq_applications_tenant_opportunity`).
- **No job without an opportunity; no opportunity without a job.**
  `OpportunityRepository.resolve_opportunity` is the only code that creates
  `opportunities` rows and it is called for every persisted job, including
  the unchanged fast path and extension captures. A new source adapter needs
  no opportunity code at all.
- **Unchanged means unprocessed.** `compute_raw_hash` on the payload as
  fetched decides whether normalisation runs at all; polling an unchanged
  board costs queries, not extraction, and never an AI call.
- **A refusal is never retried; a repeated failure opens the circuit.**
  401/403 raise immediately, 429/5xx retry with jittered backoff up to
  `MAX_RETRIES`, and `SOURCE_CIRCUIT_FAILURE_THRESHOLD` consecutive terminal
  failures skip the source for `SOURCE_CIRCUIT_OPEN_SECONDS`. Failing boards
  also back off their `next_poll_at`; nothing disables a board automatically.
- **Discovery counts, it never hides.** Malformed records are `jobs_rejected`,
  title-filtered ones `jobs_filtered`, every number is on the run row.
  Priority and fit are never consulted during ingestion.
- **A claim cites evidence or it does not ship.** Every CLAIM block and every
  generated answer carries `evidence_keys` that resolve to active,
  CONFIRMED, application-safe nodes; `TruthValidator.validate_claim` is
  called per block; numbers, years and taxonomy skills must appear in the
  cited evidence (or in the job/framing context). AI may only reword a block
  that already validates and the rewrite is validated again.
- **A missing candidate fact is a question, not a guess.** Work
  authorisation, sponsorship, relocation, notice period, salary, clearance
  and similar categories resolve from the answer bank or profile or become
  `NEEDS_USER_INPUT`; an ungroundable question becomes `NEEDS_REVIEW`.
- **Preparation is versioned by input fingerprint.** Same evidence version,
  job hash, match, level, variant version, policy, template, questions and
  AI config → the existing package is reused; anything else → a new version
  and the previous one is SUPERSEDED. Prepared is never submitted.
- **Queue transitions are owner-checked and audited.** Worker operations
  require the `claimed_by` worker id; every transition writes a
  `queue_item` audit event. Unverified/uncertain work is parked
  (BLOCKED / NEEDS_REVIEW), never retried blindly.
- **Priority orders; only the policy refuses.** `decide()` never reads
  `priority_score`; `ordering_key()` is the only consumer. A LOW-fit,
  low-priority, ELIGIBLE opportunity in an enabled band with capacity is
  admitted (regression test `test_low_fit_low_priority_eligible_is_admissible`).
- **The run window is batching, never a cap.** Admissible work beyond the
  window is WINDOW_DEFERRED and admitted by the next run; nothing is
  silently dropped (`test_window_batches_without_dropping_anything`,
  `test_no_hidden_top_n`).
- **A cap counts attempts, and only through the ledger.** Never count rows
  and then insert; `CapLedger.reserve` is the single conditional `UPDATE`
  that makes the daily/weekly caps concurrency-safe. A released attempt
  gives its slot back to the period it was taken from.
- **Cool-down and blocklist compare `company_key()`.** Raw company strings
  differ across sources ("ACME, Inc." / "acme inc"); comparing them
  unnormalised would let a blocked company through.
- **Only READY attempts are execution-ready.** `SchedulerService.ready_for_execution`
  = attempt READY, not released, with a `preparation_id`; NEEDS_USER_INPUT
  and NEEDS_REVIEW park the attempt in PREPARING. The scheduler never
  creates SUBMIT items.
- **The tenant is explicit end to end.** Match recalculation (now recorded on
  `match_runs.tenant_id`), opportunity sync, the v5 application routes and
  every scheduler/execution query take the tenant from the request; nothing
  in the pipeline falls back to `DEFAULT_TENANT_ID` on its own.
- **One logical attempt presses submit at most once.** The READY→SUBMITTING
  transition is a guarded UPDATE keyed on `submission_key IS NULL`;
  `execution_runs.submit_invoked` is committed before the executor may act.
  A crash, timeout, lost lease or late result after that point is UNKNOWN
  (attempt UNCERTAIN) and only verification or a person's confirmation can
  move it on. `retry` refuses while an UNKNOWN run is unverified. Never
  turn an ambiguous result into a resubmit.
- **A click is not a submission.** `ExecutionOutcome.SUBMITTED` needs the
  executor's evidence (confirmation page, reference, redirect); `verify`
  runs separately and may downgrade to LIKELY or FAILED. VERIFIED requires
  a verification method, never the fact that submit was attempted.
- **Execution never regenerates content.** The `ExecutionPackage` is a view
  of the READY preparation; a stale package (changed job hash, evidence
  version, variant, level, lane, cover mode, template, questions) is
  invalidated and re-prepared through the PREPARE queue, not patched. A
  policy-version bump alone is not staleness.
- **Handoff, never bypass.** CAPTCHA, login, MFA, ambiguous forms and
  unknown required fields park the attempt as BLOCKED with `blocked_reason`,
  where execution stopped and what remains; no executor may solve a
  challenge or store credentials. Voluntary/demographic questions are never
  answered from stored data.
- **State ownership is fixed.** `applications.status` = attempt lifecycle;
  `application_queue.state` = scheduling/lease; `application_preparations.status`
  = material readiness; `execution_runs.status` = one invocation's result.
  Nothing collapses two of these into one column.
- **The browser runs on the candidate's machine and never evades anything.**
  No CAPTCHA solving, no fingerprint spoofing, no proxy rotation, no stealth
  flags, no automated MFA, no stored passwords. A challenge, login wall,
  MFA prompt, unsupported widget or missing document file ends in a
  BLOCKED attempt with an exact handoff (`HandoffReason`, page, where it
  stopped, what remains). With a visible browser the executor may *wait*
  for the person to clear a challenge; it never touches the challenge.
- **Uploads are the preparation's own rendered documents, or nothing.** The
  execution service renders (or reuses) the resume / cover-letter PDF for
  the exact preparation, records the artifact ids on the run, and the
  executor re-checks the SHA-256 immediately before `set_input_files`. A
  personal file (`EXECUTION_RESUME_FILE`) is only a fallback; a missing,
  tampered or NEEDS_REVIEW document is never uploaded
  (`ARTIFACT_FILE_REQUIRED` / NEEDS_REVIEW instead).
- **Rendering is layout, never authorship.** `DocumentModel` carries the
  preparation blocks verbatim and in order; the only additions are the
  identity header from the profile, job facts already in the preparation
  and an optional configured date line. No AI, no summarising, no
  reordering. Validation fails a render in which any content line is
  missing from the extracted text (a font without the needed glyphs shows
  up here, never as silent boxes).
- **Artifacts are immutable and versioned.** A file is written once and
  never overwritten; a changed preparation, contact, option or renderer
  version is a new version; a bad or superseded artifact is INVALIDATED and
  its file kept, so an execution run always points at exactly the bytes it
  uploaded (`resume_artifact_id`, `cover_letter_artifact_id`, hashes in
  `diagnostics.artifacts`).
- **Artifact paths are built from validated identifiers only** and must
  resolve inside `documents_root`; relative paths are what the API and the
  database carry.
- **Dry-run is the default and never clicks submit** (`PLAYWRIGHT_DRY_RUN`).
  A DRY_RUN run records what would be filled; the attempt stays READY.
- **The legacy v5 submit route cannot submit.** It keeps the kill switch
  and approval checks, records a `legacy_dry_run` event for dry runs, and
  refuses live calls with a pointer to `/api/v1/execution`.
- **Caps at submission consume or refresh, never double count.** Same
  tenant-local day/week as the reservation → consumed; new period → a slot
  is taken through the ledger and the old one released; no slot → the item
  waits until the period ends (not a failure). Permanent failure and
  cancellation release; retries, handoffs and unknown results keep the slot.
- **A signal is one logical observation per tenant.** `signals.dedupe_key`
  (source + stable reference, else source + normalized content hash) is
  unique per tenant; a repeat delivery, retry or replay becomes a
  `signal_observations` row, never a second signal, a second outcome event
  (`outcome_events.dedupe_key`) or a second lifecycle transition. Unrelated
  messages are never merged because they look alike: the full normalized
  text is hashed.
- **External evidence establishes events, never candidate facts.** Nothing
  in `app/signals` writes to `evidence_nodes`, `answer_bank_entries` or a
  preparation; the AI classifier may only choose a category from the enum
  (its output is `WEAK` evidence, its identifiers are ignored). "Thank you
  for applying" makes `APPLICATION_RECEIVED` true; it never makes "5 years
  of experience" true.
- **Attribution never guesses.** A signal is linked only when exactly one
  application fits a deterministic rule; several candidates or
  disagreeing identifiers are `AMBIGUOUS` (NEEDS_REVIEW), nothing is
  `UNMATCHED`; fuzzy similarity is not a rule. A person may link later and
  the earlier decision stays, superseded.
- **Weak evidence never becomes strong evidence.** Verification keeps its
  Phase 6 meaning (VERIFIED → STRONG, LIKELY → MODERATE, UNKNOWN → WEAK,
  never upgraded by the inbox); a redirect stays weaker than a reference; a
  terminal outcome needs STRONG evidence; a weak event is `provisional` and
  never downgrades an established status; the only path from an UNCERTAIN
  attempt to VERIFIED through the inbox is `ExecutionService.verify` with a
  STRONG confirmation.
- **Outcome history is append-only.** Events and attributions are
  superseded (`superseded_by_id`) or retracted (`retracted`, with a reason),
  never updated in place or deleted; `application_outcomes` is a cache of
  the pure `derive()` and carries `rules_version` + `version`.
- **Outcomes change nothing about volume.** No threshold, cap, band,
  blocklist or policy is read or written by `app/signals`; Phase 11 consumes
  the history, and may only reorder.
- **Learning is a signal, never a gate.** `app/learning` writes only its
  own tables and audit events; the sole behavioural hook is the
  pre-existing `learned_prior` priority component, fed only when
  `learning_settings.ordering_enabled` is on (default off) and only at
  sync time. With it off, `sync_match` computes priority exactly as before
  (`learned_prior=None` → neutral 50). A test asserts the engine's source
  never mentions the policy's volume fields, and that the scheduler admits
  every admissible opportunity with ordering enabled.
- **No temporal leakage in learning.** A learning row at `as_of` uses
  only attempts created and executed by `as_of` and outcome events
  *observed* (`observed_at`) by `as_of`; employer time (`event_at`) is
  not enough. Snapshots record their `as_of`, window and versions and are
  never updated, so "what did we believe on day N" stays answerable.
- **Sparse data stays sparse.** Every learned rate carries n, positives,
  negatives, the smoothed rate (pulled toward the tenant baseline by
  `prior_strength` pseudo-observations), a Wilson interval and a
  confidence label; recommendations need ≥ MEDIUM confidence and an
  interval clear of the baseline, otherwise they say "insufficient data".
  Recommendations describe observed associations and carry a non-causal
  caveat; nothing in the codebase may phrase them as causes.

---

## 4. Extension points

- **Add a job source:** subclass `app.jobs.sources.base.JobSource` (implement
  `source_type` and `discover()`; `fetch_json()` gives you retry/backoff/rate
  limiting for free), then register it —
  `JobDiscoveryService.register_source(source_type, source_cls)` or add it to
  the `_sources` dict built in `JobDiscoveryService.__init__`.
- **Add an AI provider:** implement `app.ai.providers.AIProvider` (`name`,
  `model`, `configured`, `local`, `complete(prompt, json_mode, timeout_seconds,
  max_output_tokens) -> ProviderResult`), add it to `build_provider()` and
  `KNOWN_PROVIDERS`. Nothing else changes: callers talk to
  `AIGateway.run(AIRequest)` and get the cache, budget, schema validation,
  PII routing and accounting for free. Never instantiate a model client
  anywhere else.
- **Add an AI operation:** add a member to `app.ai.models.AIOperation`, build
  the prompt in the caller with a `prompt_version`, pass the normalized input
  (cache key) and, for structured output, a pydantic `schema_model`. Treat the
  output as a signal: validate it against the Evidence Graph / deterministic
  rules before it changes anything (see `PreparationService._polish`).
- **Add a Tier-1 gate:** add a `Gate` member and a `GateSpec` to
  `app.pipeline.gates.GATE_CATALOG` (mapping onto an existing
  `AdmissionReason`), evaluate it in `evaluate_gates()` in ruleset order, and
  **bump `GATE_RULESET_VERSION`**: candidate opportunities and audit events
  record the ruleset each decision was made under.
- **Add a signal source / connector (mailbox, extension observations, status
  pages):** build `app.signals.models.SignalIngest` (or `EmailMessage`) with
  whatever identifiers you have in `hints` and call
  `SignalIngestionService.ingest()` (`POST /api/v1/signals`, `/email`,
  `/batch` over HTTP). Supply a stable `source_reference` when the source has
  one (message id, observation id) so repeats dedupe on it; never send
  cookies, tokens or unrelated mailbox content — ingestion is scoped to what
  you pass. Nothing downstream (classification, attribution, outcomes)
  changes.
- **Add a classification rule:** append a `Rule` to
  `app.signals.classify.RULES` (named id, category, confidence, regex), put
  the category in `PRECEDENCE` and mark compatible co-matches in
  `_COMPATIBLE`, and **bump `CLASSIFIER_VERSION`** — every classified signal
  records the version.
- **Add an attribution rule:** add it to `Attributor.attribute()` in the
  strong-identifier block (it must yield a *set* of application ids; the
  exactly-one / conflict logic is shared) or before the company rule, and
  **bump `ATTRIBUTION_VERSION`**. Never rank fuzzy similarity as a match.
- **Add a learned feature or dimension:** add the field to
  `app.learning.models.LearningRow`, fill it in `dataset.build_dataset`
  (from data that existed at `as_of` only), add a `Dimension` and its
  grouper in `engine._GROUPERS`, and **bump `FEATURE_VERSION`** (rows) or
  `LEARNING_VERSION` (aggregation / recommendation logic). Changing the
  shrinkage or intervals bumps `SMOOTHING_METHOD`; changing how the
  ordering signal is formed bumps `ORDERING_VERSION`. Old snapshots keep
  their versions; never rewrite them.
- **Let learning influence something new:** don't, unless it is order or
  information. A learned signal may only feed `learned_prior` (opt-in) or a
  dashboard / recommendation; policy changes stay explicit and
  user-controlled (`PUT /api/v1/policy`), never derived from outcomes.
- **Add an outcome kind or change the derivation:** extend `OutcomeKind`,
  `STAGE` / `TERMINAL` and `OUTCOME_FOR_CATEGORY`, keep `derive()` pure, and
  **bump `OUTCOME_RULES_VERSION`**; `application_outcomes` re-derives lazily
  as events arrive (`SignalInboxService._rederive`).
- **Swap the skill taxonomy:** implement `app.intelligence.taxonomy.skills.TaxonomyProvider`
  (`canonicalize`, `find_in_text`, `related`, `category`, `all_skills`) and
  pass it to `SkillMatcher(taxonomy=...)`. See §6 for why the current
  taxonomy is a curated local table rather than ESCO/O*NET/Lightcast.
- **Add an executor (local Playwright runner, browser extension):** implement
  `app.execution.executors.base.Executor` — `can_handle(target)`,
  `prepare(package) -> FormSnapshot`, `execute(package, form, answers) ->
  ExecutionResult`, `verify(package, result) -> VerificationResult` — and
  register it in `ExecutionService(executors={...})` /
  `default_registry()`. Report honestly: `UNKNOWN` when unsure, `HANDOFF`
  with a `HandoffReason` for CAPTCHA/login/MFA, `ExecutorError(before_submit=True)`
  only when submit was certainly not pressed. An out-of-process executor
  uses the same contract over `/api/v1/execution` (`extension/match` →
  `extension/claim` (or `claim`) → `start` → `form` → `extension/gate` →
  `result` | `handoff` → `verify` | `confirm`) exactly as
  `extension/src/background.js` does; its Python half only needs
  `can_handle` + `verify` (see `executors/extension.py`). Never persist
  state yourself; never fill a field the server did not map.
- **Change form discovery:** edit `extension/src/discover.js` only — the
  Playwright executor evaluates the same file (`discovery.discover_source`),
  so both executors see identical `FormSnapshot`s; `tests/execution/
  test_playwright_discovery.py` and `test_extension_page.py` cover it.
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
| An eligible opportunity is not being enqueued | `GET /api/v1/scheduler/preview` shows its `code` and `reason` in order; `app/scheduler/decisions.py` documents the check order. Caps: `GET /api/v1/scheduler/status` (day/week used vs cap, cool-downs). |
| Daily cap looks over- or under-counted | `application_cap_ledger` (reserved − released per period) is the authority; `applications.cap_day/cap_week/released_at` say which period each attempt sits in. `app/scheduler/caps.py`. |
| Scheduler refuses to run: "already in progress" | A RUNNING `scheduler_runs` row with a fresh heartbeat; wait, or pass `force=true` after confirming the other process is dead. |
| A READY attempt is not being executed | `GET /api/v1/execution/attempts/{id}/preview` lists the failing preconditions; `app/execution/preconditions.py` documents the order. A stale package shows `PREPARATION_STALE` with the changed inputs. |
| An attempt is stuck in UNCERTAIN | An executor pressed submit and the outcome is unknown. `POST /api/v1/execution/runs/{id}/verify` (executor) or `/confirm` (person). It will never be resubmitted automatically. `app/execution/service.py:_apply_verification`. |
| A real page shows 0 fields although the form is visible in a browser | The form renders client-side or inside an embedded ATS iframe: discovery keeps re-scanning (main frame, then child frames) for `PLAYWRIGHT_SETTLE_MS`; `run.diagnostics.in_frame` / `settle_ms` say what happened. A page whose only controls are a search box or cookie buttons is `AMBIGUOUS_FORM` on purpose (a closed posting redirected to the board index). |
| An attempt hands off with CAPTCHA_REQUIRED before anything was typed | A visible challenge widget or challenge text is on the page. Run headed with `PLAYWRIGHT_HANDOFF_WAIT_SECONDS` > 0 (or use the extension): complete the widget yourself; the run continues once the provider's response token is present. Nothing solves it for you. Invisible widgets (Lever's hCaptcha, Greenhouse's reCAPTCHA badge) do not hand off up front; a challenge after the click hands off after the click. |
| An attempt is BLOCKED | `blocked_reason` says why (CAPTCHA_REQUIRED, AUTH_REQUIRED, MFA_REQUIRED, ..., COOLDOWN_ACTIVE); the run's `handoff` says where it stopped and what remains. Finish it by hand, then `/confirm` or `/retry`. |
| An executor reports "Item is ... owned by ..." | Its lease lapsed and another worker reclaimed the item; the late result is refused on purpose. `QueueRepository._assert_owner`. |
| The worker says playwright is not installed / cannot launch | `pip install -e ".[browser]"` then `python -m playwright install chromium` (local, ~150 MB). `app/execution/playwright/browser.py:BrowserSession.start`. |
| Every attempt ends as DRY_RUN | `PLAYWRIGHT_DRY_RUN=true` (the default). Run the worker with `--live` once a dry run looked right. |
| An opportunity is not admitted and you want to know which rule | `GET /api/v1/opportunities/...` shows `policy_reason`; the admission audit event (`candidate_opportunity` / `admission`) carries the full Tier-1 gate report (`after.gates`: every gate, passed, code, detail, ruleset + policy version). `GET /api/v1/policy/gates` lists the ruleset with current parameters. `app/pipeline/gates.py`. |
| A candidate opportunity shows a band that does not match today's thresholds | Expected until the next sync: `fit_policy_version` says which policy version derived it; stored matches are never rewritten. `POST /api/v3/matches/recalculate` or `POST /api/v1/opportunities/sync` re-bands under the current version. |
| AI never runs / L2 packages show `ai_used=false` | `GET /api/v1/ai/config` → `effective.reason` says why: `AI_ENABLED` false, the tenant switch off (`policy.ai_settings.enabled`), provider unconfigured (no key), or a hosted provider not allowed for candidate data (`AI_CANDIDATE_DATA_PROVIDERS`). Every case is by design a deterministic fallback, never an error. |
| AI calls stop mid-run with BUDGET_EXHAUSTED | `AI_MAX_CALLS_PER_RUN` (or the tenant's narrower `max_calls_per_run`) per discovery run / preparation package. Cache hits are free. `GET /api/v1/ai/status`. |
| An AI answer looks stale after a prompt change | Bump the caller's `prompt_version` (or `schema_version`); the cache key includes both, so old entries become unreachable. `POST /api/v1/ai/cache/clear` drops everything. |
| An L2 rewrite you expected was not applied | The preparation's audit event (`ai.rejected_by_truth_gate`) counts rewrites the validator refused (unsupported claim, number, employer, skill). This is the guardrail working: AI never becomes evidence. |
| The executor fills nothing on a page you can see is a form | Check `execution_runs.diagnostics.fields_discovered`; the discovery script skips hidden/disabled controls and custom widgets. Compare the labels it found (`form_fields`) with the page; add the ATS's field names to `strategies.py`. |
| A form ends BLOCKED with ARTIFACT_FILE_REQUIRED | The rendered document vanished or changed between `start` and the upload (hash mismatch), or the preparation has no cover letter for a required cover-letter field. `GET /api/v1/documents/by-preparation/{id}`, then retry. |
| An attempt ends NEEDS_REVIEW with "documents: …" | Rendering or validation refused: e.g. `7 pages exceeds the 4-page bound` (`DOCUMENTS_MAX_PAGES_RESUME`), a missing content line, or content needing a Unicode font (`DOCUMENTS_FONT_PATH`). Inspect the artifact's `validation_report`. |
| Two renders of the same preparation give different bytes | They should not: check the renderer version, the font actually used (`fingerprint` includes it) and the `documents_letter_date` setting. `tests/documents/test_rendering.py::test_rendering_is_byte_deterministic`. |
| Generated files are showing up in `git status` | They live under `data/documents/` (git-ignored). Any other location means `DOCUMENTS_ROOT` was pointed inside the tree; move it. |
| A submission is UNCERTAIN after the browser closed | Expected: the click may have gone through. Check the employer site / email, then `/confirm`. Never re-run it blindly. |
| The extension popup says "not connected" | Options → server address must be `http://127.0.0.1:PORT` or `http://localhost:PORT` and the API key must match `API_KEY`; *Test connection* calls `/api/v1/execution/extension/status`. A 401 is the key; "not reachable" is the address or the server. |
| The extension badge never shows on an application page | The tab URL is matched against the job's `application_url` / `source_url` (scheme, `www.`, query, fragment ignored; prefix either way): `GET /api/v1/execution/extension/match?url=…`. The item must be a runnable SUBMIT item (scheduler run + `enqueue-ready`), not held by another live worker. `ExecutionService.match_for_url`. |
| The person clicked submit but nothing happened; a banner says "not submitted" | The pre-submit gate refused (`/extension/gate` failures are in the banner and in the run's `diagnostics.gate_failures`); the attempt is NEEDS_REVIEW with the reason. Dry-run mode also blocks the click, by design. |
| The extension filled nothing / "no fillable form controls" | Same discovery script as the runner: hidden/disabled controls and custom widgets are skipped; the page was handed off as `UNSUPPORTED_FORM`. Inspect `form_fields` for what was found. |
| Submit went through on the site but CareerOS shows UNCERTAIN | The confirmation page was on another origin and the extension had no permission to read it (grant "all sites" in options), or the tab was closed. `/confirm` from the popup or the dashboard. Never resubmitted. |
| An employer email did not change anything | `GET /api/v1/signals?review=true` (or `/dashboard/signals?review=1`): the signal is NEEDS_REVIEW (ambiguous attribution, weak or unknown classification) or UNMATCHED (no application fits); `status_reason` and the attribution's `explanation` say which. Link it or confirm the classification; the outcome is recorded then. `app/signals/attribution.py`, `classify.py`. |
| Two applications to the same company; the signal went to review | Expected: company-only evidence is AMBIGUOUS; the email must carry the title, a reference number, the application URL or be a reply in an attributed thread. Link it by hand once; replies then follow the thread. |
| A rejection did not move the attempt to REJECTED | The event is there (`GET /api/v1/signals/outcomes/{application_id}`) but its evidence is MODERATE / WEAK (MEDIUM rule confidence or an AI suggestion): terminal outcomes need STRONG evidence. Confirm the outcome (human = STRONG). `app/signals/outcomes.py`. |
| `application_outcomes.current_outcome` is NEEDS_REVIEW | `conflicts` lists why: progress evidence after a terminal outcome, two different terminal outcomes. Retract the wrong signal (ignore / reject classification) or confirm the right outcome. History is never rewritten. |
| The same email was ingested twice | It was not: `observation_count` / `signal_observations` show the repeats; one `dedupe_key` per tenant. If two *different* message ids carry the same text they are two signals — `merge` one into the other. |
| An UNCERTAIN attempt got a confirmation email but stayed UNCERTAIN | The signal must be a STRONG confirmation (rule confidence HIGH) attributed with HIGH confidence (id, reference, URL, thread or company+title); company-only (MEDIUM) does not verify. Confirm the outcome or `/confirm` the run. `SignalInboxService._maybe_verify`. |
| AI classified nothing | By design unless `AI_ENABLED` and the tenant's `ai_settings.enabled` are on *and* the rules were not confident; `signals.ai` holds the gateway status (`DISABLED`, `BUDGET_EXHAUSTED`, `REFUSED` for a hosted provider on candidate data, `MALFORMED` for a category outside the enum). |
| The extension's gate returns `LEASE_EXPIRED` | The person took longer than `EXECUTION_LEASE_SECONDS` on the page without a heartbeat. The extension heartbeats while a job is active; a manual retry of the gate after a heartbeat passes if nobody reclaimed the item. Never bypassed: an expired lease cannot authorise a click. |
| Attempts show RUNNING runs after a crash / restart | Startup (`app.main` lifespan) and every `run_queue` call `recover_execution` / `recover_lost_runs`: lease lapsed + submit invoked → UNCERTAIN (verify or confirm); lease lapsed before submit → READY again; live lease → left to its worker. `GET /api/v1/ops/diagnostics` lists RUNNING runs and stale leases. |
| Something looks stuck and you want one page of truth | `GET /api/v1/ops/diagnostics` or `/dashboard/ops`: queue by state, stale leases, uncertain / blocked / review counts, RUNNING runs, 24 h throughput, signals awaiting review, cap usage, kill switches, warnings. See `docs/OPERATIONS.md`. |
| The learning page shows a company at 100% from one application | Expected and labelled: `n=1`, confidence LOW, the smoothed rate sits near the baseline and the interval spans most of 0–1. No recommendation is made below `min_samples`. `app/learning/stats.py`. |
| Learning "ignored" an outcome you can see in the inbox | The event's evidence is below `learning_settings.minimum_evidence` (MODERATE by default: AI-only / LOW events are exploratory), or it was retracted / superseded, or it was observed after the snapshot's `as_of`. `GET /api/v1/learning/dataset` shows `evidence_counts` and `events_below_threshold` per row. |
| Priority scores did not change after computing a snapshot | Ordering is opt-in: `learning_settings.ordering_enabled` (dashboard or `PUT /api/v1/learning/settings`), then the next sync (`POST /api/v1/opportunities/sync`) feeds `learned_prior`; `priority_scores.components.learned` records the snapshot used. Admission never changes either way. |
| Fewer applications after enabling learning | Not possible from `app/learning`: it never touches caps, bands, thresholds, blocklists or admission (`tests/learning/test_safety.py`). Look at the Phase 5 reasons (`GET /api/v1/scheduler/preview`): cool-down, duplicates, caps. |
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
- **No hosted cache.** The AI answer cache (`app.ai.cache.AICache`, owned by
  the gateway) is a plain content-addressed directory of JSON files under
  `.cache/ai/` — keyed by gateway version, operation, prompt/schema version,
  provider/model, normalized input and (candidate-side) tenant — works on any
  host with a writable disk, no Redis/Memcached dependency. (The Phase 2
  `.cache/llm/` directory is no longer read.)
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
- **One AI Gateway** (`app.ai.gateway.AIGateway`, Blueprint Phase 8b / §7):
  every model call in the codebase goes through it — job-side extraction and
  the L2 polisher today, anything later. It owns provider selection (stub /
  Gemini free tier / local Ollama), the versioned cache, per-run call
  budgets, output-schema validation, the PII class (candidate-side text only
  to providers listed in `AI_CANDIDATE_DATA_PROVIDERS` or local ones,
  tenant-private cache) and accounting (`ai_usage`). `AI_ENABLED=false` is
  the default and every caller has a deterministic path, so the whole
  pipeline runs with zero AI calls.
- **Deterministic-first.** `LLMFallbackExtractor` only asks the gateway for
  fields deterministic extraction left `UNKNOWN`; `RequirementExtractor`
  (Phase 3) never calls an LLM at all; L0/L1 preparation never does; L2 only
  rewords; the Signal Inbox classifies by rules and asks the gateway only
  for the ambiguous long tail (and only when the tenant turned AI on). Most
  jobs and most signals cost zero API calls end to end.
- **No ML framework for learning.** Phase 11 is counts, a beta-binomial
  posterior mean, Wilson intervals and medians in plain Python over a few
  thousand rows; it runs in seconds on the laptop, needs no AI call, and
  every number is reproducible from its snapshot's versions and window.
- **No mailbox or browser infrastructure for signals.** Emails and page
  observations are *supplied* (`POST /api/v1/signals/...`) by the person, the
  extension or a future local connector; there is no IMAP/Gmail/Outlook
  integration and no cloud email worker. Storage is a bounded excerpt with
  hashes, never a mailbox copy; `SIGNAL_EXCERPT_RETENTION_DAYS` +
  `purge_excerpts` keep it that way.

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
