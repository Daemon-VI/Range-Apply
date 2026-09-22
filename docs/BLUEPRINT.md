# CareerOS / Range Apply — Revised Blueprint (source of truth)

**Status:** authoritative for all implementation from September 2026 onward.
**Relationship to the PRD:** the original *Range Apply / CareerOS PRD* (P1–P6,
FR-01–FR-12) remains the foundation. This document does not replace it; it
records the improvements layered on top of it and the decisions that resolve
conflicts. When the two disagree, this document wins; when this document is
silent, the PRD applies.

---

## 0. Product objective

**Primary objective: maximise successful, truthful, relevant job application
throughput.** The candidate wants to apply to the maximum number of legitimate,
eligible, relevant jobs, while maintaining eligibility, quality, factual
accuracy, personalisation, duplicate prevention, reliability, low cost and
user control.

**Primary metric:** verified relevant applications per day and per week.
"Interviews per hour" is a secondary diagnostic only; nothing in the system
may lower application volume in pursuit of it.

Non-negotiable rules:

1. Never fabricate candidate information. Every generated sentence resolves to
   a confirmed evidence node; identity, authorisation, dates, salary and
   education fields are copied, never generated.
2. Never bypass CAPTCHAs, anti-bot systems, employer restrictions or
   application requirements. On a bot check: pause, notify, hand control to
   the human.
3. AI is never required for the core pipeline. Discovery, eligibility, fit,
   ordering, L0/L1 tailoring, execution, verification, tracking and learning
   all run with zero model calls.
4. Deterministic processing runs before any model call.
5. The hard free-tier constraint holds: no paid infrastructure is required to
   run the MVP. AI cost is the only variable cost and is budgeted per tenant.
6. Eligible jobs in a band the candidate has enabled are never silently
   skipped. Prioritisation decides *order and processing strategy*, never
   *whether*.
7. Learning may reorder, reallocate tailoring depth, change source cadence and
   suggest. It may never lower daily caps, thresholds, enabled bands or volume.

---

## 1. What is preserved from CareerOS (all of it)

Career Brain and structured profile · evidence and provenance · Greenhouse,
Lever, Ashby adapters · Firecrawl as bounded optional fallback (never for
submission) · RawJob storage, normalisation, dedup, versioning, provenance
(source, URL, fetched-at, parser version, content hash) · eligibility separated
from relevance with ELIGIBLE / LIKELY_ELIGIBLE / UNCERTAIN / INELIGIBLE ·
configurable, explainable, versioned fit scoring · tailoring with evidence refs
and versioned artifacts · ATS adapters, Playwright, field mapping, uploads ·
approval gates · idempotency · verification · bounded retries · application
state machine and audit events · observability and run history · scheduler
(GitHub Actions cron) · provider abstraction · security rules · kill switch and
scoped pause · testing strategy · Alembic-owned schema · Jinja2 dashboard.

---

## 2. Limitations being fixed

| # | Limitation in the PRD as built | Fix |
|---|---|---|
| L1 | Linear pipeline without cost tiers | Progressive Tier 1–5 pipeline (§4) |
| L2 | Per-job tailoring is the only mode | Tailoring levels L0/L1/L2 + positioning variants (§6) |
| L3 | Approval is per action → human is the bottleneck | Policy lanes AUTO / REVIEW / MANUAL + spot checks (§8) |
| L4 | Truthfulness is a requirement, not a mechanism | Per-sentence evidence ids + blocking validator (§7) |
| L5 | Server-side Playwright | Playwright moves to a **local runner**; browser extension is the second executor; headless login-free lane is optional (§8) |
| L6 | No shared job-side computation | Shared market tables + shared AI cache; tenant tables for everything candidate-side (§3) |
| L7 | Tracking without outcome ingestion or learning | Signal inbox + learning engine (§9, §10) |
| L8 | Discovery breadth is thin | ATS auto-detect, company-list growth, aggregator/RSS adapters, extension capture (§5) |
| L9 | Ranking without prioritisation or timing | Priority scheduler with freshness, deadlines, priors (§8) |
| L10 | Verification relies on confirmation pages | Confirmation email matching + already-applied detection (§9) |
| L11 | Answers generated per job | Candidate-curated answer bank with deterministic slot fill (§6) |
| L12 | No AI cost architecture | AI gateway: rules → cache → PII class → route → budget → validate → account (§7) |
| L13 | Dedup stops at the job record | Opportunity-level identity, repost detection, cool-down (§5) |
| L14 | Dashboard for inspection, not throughput | HTMX keyboard review queue on the same Jinja2 stack |
| L15 | Scale phase assumes paid infrastructure | Postgres queue + GitHub Actions batch + execution in the candidate's browser |
| L16 | No anti-bot policy | Detect → pause → notify → human completes; never solve |

## 2b. Vantage concepts — verdicts

KEEP/INTEGRATE: evidence graph; structural truthfulness; shared market plane;
lazy AI; AI gateway with PII split, budgets, caching; local models and
cassettes; free-tier architecture; browser extension; signal inbox; answer
bank; tailoring levels.
MODIFY: learning engine (volume-neutral); campaign planner → priority
scheduler (order only); daily plan → today's runs and review queue;
opportunity/path → opportunity identity + optional outreach attached to an
application; fit brief → explanation panel.
POSTPONE: recruiter/hiring-manager discovery; PWA frontend; interview and
offer coaching; company dossiers.
REJECT: "interviews per hour" as the objective; system-imposed time budgets
that reduce volume; "no server-side browsers ever" (Playwright is retained,
relocated to the local runner).

---

## 3. System architecture

```
┌──────────────────────────── CANDIDATE SIDE ─────────────────────────────┐
│ Jinja2+HTMX dashboard [CORE]  Browser extension [CORE]  Local runner [CORE] │
│  review queue, controls,       capture, fill, submit,    Playwright, own    │
│  runs, metrics, evidence       batch mode, signal log    profile, tabs      │
└──────────────┬───────────────────────┬──────────────────────┬─────────────┘
┌──────────────▼───────────────────────▼──────────────────────▼─────────────┐
│ API + INTERACTIVE WORKER [CORE]  FastAPI · policy engine · ledger · queue   │
└──────────────┬────────────────────────────────────────────────────────────┘
┌──────────────▼────────────────────────────────────────────────────────────┐
│ POSTGRES [CORE] (SQLite in solo mode)                                      │
│  TENANT: candidate_profiles, evidence_nodes, positioning_variants,          │
│          answer_bank, eligibility_decisions, match_scores, priority_scores, │
│          tailored_artifacts, applications, application_queue, attempts,    │
│          verifications, signals, outcomes, audit_events, ai_cache_tenant   │
│  SHARED: companies, job_sources, raw_jobs, jobs, job_versions,             │
│          job_requirements, opportunities, field_maps, email_patterns,      │
│          ai_cache_shared, priors                                           │
│  SYSTEM: queue, ingestion_runs, ai_requests, ai_budgets, ai_usage, metrics │
└──────┬──────────────────────────┬───────────────────────────┬─────────────┘
┌──────▼──────────────┐ ┌─────────▼───────────────┐ ┌─────────▼──────────────┐
│ BATCH WORKER [CORE] │ │ AI GATEWAY [CORE]       │ │ SIGNAL INBOX [CORE]    │
│ GitHub Actions cron │ │ rules→cache→PII→route   │ │ paste, extension log   │
│ discovery, dedup,   │ │ →budget→model→validate  │ │ CF Email Worker [OPT]  │
│ extraction, embed,  │ │ providers: cassette,    │ │ patterns → small model │
│ enrichment, pruning │ │ Ollama, free hosted     │ └────────────────────────┘
│ headless lane [OPT] │ │ (job-side), paid        │
│ Firecrawl [OPT]     │ │ no-training (PII)       │
└─────────────────────┘ └─────────────────────────┘
FUTURE SCALE: paid Postgres tier, always-on batch VM, PWA, trained priors
```

**Two deployment modes, one codebase.** `DEPLOYMENT_MODE=solo` runs everything
on the candidate's machine (the original single-user CareerOS posture, SQLite
or local Postgres, headed Playwright). `hosted` uses Supabase + Render +
GitHub Actions + Cloudflare, with all submissions in the candidate's browser.

**Hard rules:** no Redis, Kafka, Kubernetes, cloud browser fleets or a second
database without a demonstrated, measured requirement. Postgres is the queue.

---

## 4. Progressive pipeline

```
DISCOVERY → NORMALISE → DEDUP → TIER 1 → TIER 2 → TIER 3 → TIER 4 → TIER 5 → VERIFY → TRACK → LEARN
```

| Tier | What | Cost | Output |
|---|---|---|---|
| 0 | Discovery + opportunity identity | SQL, hashes | Opportunity |
| 1 | Hard eligibility, deterministic: work authorisation, location, work mode, graduation window, years, degree/licence, language, salary floor, blocklist, cool-down, already-applied | zero | ELIGIBLE / LIKELY / UNCERTAIN / INELIGIBLE. INELIGIBLE stops. |
| 2 | Fast fit: skills taxonomy, title family, seniority, years, embeddings (optional, local), freshness | zero | fit 0–100 + band |
| 3 | AI only when it changes an outcome: uncertain eligibility field in an enabled band; high-band real-bar synthesis; novel free-text question | bounded by high-band + uncertain count | enrichment, shared and cached |
| 4 | Tailoring strategy by band, ATS, question set | L0 zero, L1 zero/small, L2 model | artifacts |
| 5 | Execution via lanes and executors | zero infra | submission + verification |

**Bands** (thresholds are candidate settings): HIGH (>70 default), MEDIUM
(45–70), LOW-ELIGIBLE (<45 but eligible; enabled by default in aggressive
mode), EXCLUDED (fails a user rule). An eligible job in an enabled band always
enters the queue.

---

## 5. Discovery, identity, dedup

Sources: public ATS boards (Greenhouse, Lever, Ashby, then more), ATS
auto-detect from a company list that grows from every candidate target,
captured page and aggregator hit; aggregator and RSS adapters; extension
capture (JSON-LD `JobPosting` first, DOM fallback); Firecrawl bounded and
optional (self-hosted or hosted key).

**Opportunity identity is distinct from source identity.** Job rows keep
`(source, source_job_id)`; an `Opportunity` groups job rows that are the same
hiring need. Resolution, strongest first, exact-equality only, never fuzzy
merging: (1) source job id, (2) canonical URL, (3) company + normalised title
+ location bucket, (4) title cluster within company, (5) simhash/content hash
within company. Reposts (same opportunity re-listed after closure) are
detected and linked, not treated as new. Configurable company/job cool-down
blocks re-application within a window, with an explicit override. **Duplicate
prevention is enforced by a unique constraint on the application queue per
`(tenant, opportunity)`, not by the UI.**

---

## 6. Tailoring levels and answer bank

| Situation | Level | AI |
|---|---|---|
| LOW-eligible band, standard form | L0: pre-validated positioning variant + answer bank | none |
| MEDIUM band | L1: deterministic reorder, keyword-aligned templated summary, answer bank | none or small |
| HIGH band | L2: L1 + rewrite of top bullets with evidence ids, optional letter | mid/frontier, cached per job × positioning |
| Novel essay question, any band | draft to review queue | mid, tenant cache |
| Warm contact at company | same level + optional outreach draft | mid |
| Deadline < 48 h | priority raised, level unchanged | none |

Positioning variants (one per role family) are evidence-validated once and
versioned. The answer bank holds candidate-approved answers for repeating
questions (authorisation, sponsorship, relocation, notice period, salary,
work mode, education, years, common prompts) filled deterministically; novel
questions route to AI or review. When AI budget is exhausted, L2 degrades to
L1, never to skipping.

---

## 7. Truthfulness and the AI gateway

**Structural validator (blocking).** Every generated sentence carries evidence
node ids. Reject: missing or unresolvable node; number differing from the node;
use of a removed or unconfirmed node; inconsistent dates; any claim of
experience, company, degree, authorisation, salary or metric not present in
the graph. Identity fields are copied from structured data. An artifact that
fails validation cannot enter the application queue.

**Gateway flow:** RULES → CACHE → PII CLASSIFICATION → PROVIDER ROUTING →
BUDGET CHECK → MODEL → VALIDATION → CACHE → ACCOUNTING. Every request records
task, input hash, template version, provider, model, tokens, estimated cost,
latency, cache hit/miss, tenant, application id, failure class. Job-side
tasks (no PII) may use any provider and a shared cache; candidate-side tasks
(PII) only providers meeting the configured privacy class or local models,
tenant cache. Providers: cassette/mock, Ollama/local, free hosted (job-side),
paid no-training. Structured outputs validated against schemas, bounded
retries. Caches are keyed by version, never expired by TTL.

**Degradation:** L2→L1; narrative→structured explanation; unknown question→
review queue; uncertain eligibility→human review. The pipeline never stops.

---

## 8. Application engine, lanes, executors, scheduler

**State machine** (PRD, extended): DISCOVERED → QUALIFIED → SHORTLISTED →
PREPARING → READY → AWAITING_APPROVAL → SUBMITTING → SUBMITTED | UNCERTAIN |
FAILED | BLOCKED_BY_BOT_CHECK → VERIFIED (page / id / email / portal) →
INTERVIEWING | REJECTED | OFFER | CLOSED. Unverified applications are never
resubmitted automatically.

**Queue:** one Postgres table, `SELECT … FOR UPDATE SKIP LOCKED`, idempotency
key, `run_after`, attempts, backoff, dead state. All executors consume it:
(1) browser extension in the candidate's session, (2) local Playwright runner
with a persistent profile and parallel tabs, (3) optional login-free headless
lane on GitHub Actions (off by default), (4) API adapter where an employer key
exists.

**Lanes:** AUTO (policy satisfied: eligible, band in allowed set, ATS in
allowed set, all questions answerable, no essays, above salary floor; spot
check 1-in-N surfaced afterwards), REVIEW (keyboard-driven queue, ~10 s per
item target), MANUAL (extension highlights fields to paste and records the
submission). Caps and limits: daily cap (candidate), per-ATS concurrency 1,
minimum spacing per company, per-source polling limits, jitter, global kill
switch and per-lane pause.

**Priority scheduler** (deterministic, weights configurable): fit + freshness +
deadline urgency + source reliability + learned priors − execution cost when
capacity is scarce. Decides order and strategy only.

**Scaling path:** 10/day review-only → 50/day AUTO lane on allowed ATS + answer
bank → 100/day local runner overnight, aggregators on → 500/day runner with
4–6 tabs, low band on, L0 dominant. Infrastructure is unchanged between rows.
The eventual bottleneck is market supply for one candidate's criteria, then
employer bot checks and per-company spacing, then review capacity for novel
questions.

---

## 9. Verification and signal inbox

Verification sources: confirmation page pattern per ATS, application id,
on-page "already applied" detection, confirmation email matched by sender,
company and time window, portal status read by the extension. Signal inbox
inputs: pasted email, extension logging, optional Cloudflare Email Worker.
Deterministic sender/subject patterns first; a small model only for unknown
senders. Signals update application states; ghost inference after a
family-specific silence window.

---

## 10. Learning engine (volume-neutral)

Tracked per application: source, company, role family, title cluster, ATS,
lane, tailoring level, positioning variant, fit band, warm path, submission
time, verification method, response type and latency, interview stages,
offer, final outcome.

May change: queue ordering, source polling cadence (never off unless the
candidate disables), tailoring allocation under scarce budget, positioning
default per family (with minimum sample), rule *suggestions*.
May not change: daily caps, enabled bands, thresholds, application volume.
Cross-user learning uses de-identified aggregates only.

---

## 11. Metrics

Primary: verified relevant applications per day/week. Secondary: eligible
discovered, prepared, submitted, submission success rate, verification rate,
failed submissions, retries, bot checks per ATS, duplicate rate, validator
rejection rate, response rate, interview rate, interviews/application,
interviews per 10 candidate hours, offer rate, AI calls, cache hit rate, AI
cost/application, infrastructure cost/application, candidate minutes per
application, time-to-response by source.

---

## 12. Security boundaries and human control points

PII only in tenant tables and the candidate's own browser/machine. Job-side
tables contain no PII. Candidate-side model calls only to privacy-class
providers or local models. No ATS credentials on the server; sessions live in
the candidate's browser profile. Extension talks only to the API origin and
fetches artifacts per item. Secrets in environment; logs never carry
document text, cookies or tokens (enforced by `app/core/logging.py`). Every
automated submission has an audit event with policy version, artifact
versions and executor.

Control points: evidence grading, answer bank approval, aggressiveness
(thresholds, bands, cap, cool-down), lane policy, review queue, spot checks,
bot-check pause, kill switch and per-lane pause, learning suggestions
(accept/ignore), export and delete.

---

## 13. Implementation phases (vertical slices, each runnable)

| Phase | Scope | Status |
|---|---|---|
| 0 | Repository and development foundation: assessment, structured errors, secret-redacting logging, config (deployment mode, tenant, pool), docker-compose Postgres, CI on pgvector image, lint green, this document | **done 2026-09-11** |
| 1 | Career Brain + Evidence Graph: DB-backed evidence nodes with grades and provenance, seed import from `career_seed.json`, positioning variants, answer bank, audit ledger, tenant scoping, edit API + dashboard | **done 2026-09-11** |
| 2 | Multi-tenant persistence, opportunity model, queue foundation: shared `opportunities` + `opportunity_jobs`, tenant `candidate_opportunities`, `eligibility_decisions`, `priority_scores`, `application_policies`, `application_queue`; deterministic priority; policy admission; match→opportunity sync; `/api/v1/{opportunities,policy,queue}`; inspection pages | **done 2026-09-11** |
| 3 | High-volume discovery: raw-hash unchanged fast path, validation, failure kinds + jitter + circuit breaker, per-run volume/AI metrics, resumable runs, source health with polling back-off and `run-due`, freshness classification, repost detection (record and opportunity level), extension capture contract + endpoint, cheap post-run candidate projection, 5,000-job synthetic fixture. ATS auto-detect, aggregator adapters and company-list growth are deferred to Phase 4+. | **done 2026-09-11** |
| 4 | Evidence-backed application preparation (pulled forward from the original Phase 6 slot): tenant/opportunity-scoped `application_preparations` with versions and input fingerprints, tailoring levels L0/L1/L2, deterministic positioning selection, evidence snapshot + selection, resume/cover-letter blocks with per-block evidence keys, answer bank + profile + templated answers with explicit NEEDS_USER_INPUT, `TruthValidator`-backed structural validation, optional L2 polish that cannot bypass the gate, PREPARE queue worker, API + dashboard, 1,000/5,000 zero-AI benchmark | **done 2026-09-12** |
| 5 | Deterministic scheduler + real policy enforcement: one policy engine with reason codes (`AdmissionReason`), attempt reservation as the unit caps count (atomic `application_cap_ledger`, tenant-timezone day/ISO-week), normalised company cool-down and blocklist, opportunity-level duplicate and repost handling under `duplicate_policy`, priority-then-deadline-then-freshness-then-id ordering, idempotent PREPARE enqueueing, run window as batching (never a cap), `scheduler_runs` records, reconcile of in-flight attempts (READY / released), orchestration of the Phase 4 worker, execution-readiness gate (`ready_for_execution`), tenant-explicit match runs, `/api/v1/scheduler`, `/dashboard/scheduler`, 500/5,000 mixed-population benchmark. SUBMIT items are created by the execution engine (Phase 8) from READY attempts, never by the scheduler. | **done 2026-09-13** |
| 6 | Execution foundation (pulled forward; the original Phase 6 items move to 7+): canonical `ExecutionPackage` from a READY preparation keyed by tenant / candidate opportunity / opportunity / preparation / attempt; attempt state machine (`ATTEMPT_TRANSITIONS`: READY → SUBMITTING → SUBMITTED → VERIFIED with BLOCKED / NEEDS_USER_INPUT / NEEDS_REVIEW / UNCERTAIN / FAILED / CANCELLED); `execution_runs`, `form_snapshots`, `form_fields`; `Executor` contract (`can_handle / prepare / execute / verify`) with MOCK and MANUAL executors; submission-time preconditions (stale preparation by input fingerprint, blocklist, cool-down, duplicates, closed opening, missing candidate facts, cap re-check with reservation consume/refresh); guarded READY→SUBMITTING mutex + `submit_invoked` marker (one submit per attempt; unknown results go to verification, never a resubmit); verification contract; human handoff (CAPTCHA / AUTH / MFA / ambiguous / unknown field / user confirmation) that pauses; SUBMIT queue items on the existing queue; `/api/v1/execution`, `/dashboard/execution`; `match_runs.tenant_id`; 1,000-attempt mock benchmark. No browser automation. | **done 2026-09-14** |
| 7 | Local Playwright executor (the first real `Executor`): browser on the candidate's machine only, `BrowserSession` lifecycle with explicit timeouts and clean shutdown, DOM form discovery into canonical `FormSnapshot`s (structure + stable selectors, no HTML), ATS strategies (Greenhouse, Lever, Ashby-with-standard-controls, generic) isolated from the executor, safe field mapping (exact question → bank/prepared → category; never a guess), typed filling (text/textarea/email/phone/select/radio/checkbox/multi-select/numeric/date/file), uploads only of real local files (`EXECUTION_RESUME_FILE`, else `ARTIFACT_FILE_REQUIRED` handoff), pre-submit gate re-running every Phase 6 precondition, `submit_invoked` before the click, conservative post-submit classification (confirmation / validation error / CAPTCHA / UNKNOWN), verification from page evidence only, CAPTCHA / login / MFA / unsupported-widget handoffs, dry-run default, local worker `python -m app.execution.worker`, legacy v5 submit reduced to a compatibility wrapper, 100-form local fixture benchmark | **done 2026-09-14** |
| 8 | Local document artifact pipeline: validated Phase 4 preparations rendered deterministically to PDF (fpdf2) and DOCX (python-docx) from the same `DocumentModel`, immutable versioned files under a tenant/opportunity/preparation path, SHA-256 + input fingerprint + renderer version in `document_artifacts`, structural and content-equivalence validation (no line of content may be lost; oversize → NEEDS_REVIEW), reuse of unchanged artifacts, invalidation instead of mutation, `/api/v1/documents`, execution uploads the preparation's own rendered PDF with a hash check immediately before upload and records the exact artifact ids on the run, pre-submit cap rollover now refreshes the reservation through the ledger (waits when the new period is full), 100-resume + 100-cover-letter benchmark, zero AI | **done 2026-09-15** |
| 8b | Tier-1 gate set as one ordered, versioned, all-reporting ruleset (`app/pipeline/gates.py`, `tier1-gates-v1`: candidate skip, opening open, evaluated, hard ineligibility, minimum eligibility, blocklist, scored, band enabled, fit floor; stateful gates catalogued where they are enforced) mapped onto the existing `AdmissionReason` codes — priority never consulted, no top-N; fit bands as versioned candidate settings (`/api/v1/policy/fit-bands`, `/gates`) with `fit_policy_version` / `admission_policy_version` / `gate_ruleset_version` recorded on every candidate opportunity and the full gate report on the admission audit event, so a threshold or rule change never rewrites a stored match or an earlier decision; one AI Gateway (`app/ai`: `AIGateway.run(AIRequest) -> AIResponse`, providers stub / Gemini / local Ollama, content-addressed versioned cache — tenant-private for candidate data —, per-run budgets, schema validation, credential-shaped prompts refused, `ai_usage` accounting, `/api/v1/ai/*`, per-tenant `ai_settings` on the policy that can only narrow the global `AI_ENABLED=false` default); the Phase 2 extractor and the Phase 4 L2 polisher are its only callers, every failure mode degrades to deterministic output, and AI can never create evidence (rewrites re-validated, unsupported claims rejected). No new infrastructure. | **done 2026-09-16** |
| 9 | Browser extension as the second `Executor` (`extension/`, Manifest V3, vanilla JS, no build step): the candidate's own browser drives the existing Phase 6 API — tab URL → `/extension/match`, guarded `/extension/claim`, `start` (BROWSER_EXTENSION), shared `discover.js` (one discovery script for Playwright and the extension) → `form` (the server maps every answer), rendered documents fetched from `/api/v1/documents/{id}/file?for_upload=true` and re-hashed before upload, typed filling of safe answers only, every submit (the person's click or the extension's) intercepted and released only by `/extension/gate` (all preconditions + kill switch, `submit_invoked` recorded), page evidence → `result` → `BrowserExtensionExecutor.verify` (reference / confirmation text → VERIFIED, redirect → LIKELY, nothing → UNKNOWN → UNCERTAIN), CAPTCHA / login / MFA / unsupported form / missing document → handoff with popup confirmation, unanswered fields → NEEDS_USER_INPUT answered from the popup through the answer bank, submit modes manual / auto / dry-run, CORS for `chrome-extension://` + `moz-extension://` only, API key in extension storage, 50-form local benchmark. Signal log and controls beyond the popup remain Phase 10. | **done 2026-09-16** |
| 10 | Verification + Signal Inbox + outcome attribution (`app/signals`): persistent tenant-scoped `signals` deduplicated on a stable reference or the normalized content hash (repeats become `signal_observations`, never a second signal; bounded, credential-redacted excerpt, never raw HTML/headers/cookies); sources EXECUTION / EMAIL / STATUS_PAGE / MANUAL / EXTENSION / PLAYWRIGHT; deterministic rule classifier (`signal-classifier-v1`: confirmation, received, rejection, interview, assessment, recruiter, information request, withdrawal, duplicate/closed, status update, other, unknown) with precedence and confidence; optional AI only through the Phase 8b gateway (`CLASSIFY_SIGNAL`, candidate scope, enum-only output, never above MEDIUM, off by default); deterministic attribution (`signal-attribution-v1`: application / run / opportunity / job ids → email thread → confirmation reference → embedded ids → application URL → company+title, exactly-one-or-AMBIGUOUS, conflicts → NEEDS_REVIEW, nothing → UNMATCHED) with an append-only `signal_attributions` history; append-only `outcome_events` with evidence strength (STRONG deterministic-HIGH / VERIFIED / human; MODERATE MEDIUM / LIKELY; WEAK AI / LOW / UNKNOWN) and `application_outcomes` derived by explicit rules (`outcome-rules-v1`: highest stage wins, terminal needs STRONG, weak never downgrades, progress-after-terminal and two terminals → NEEDS_REVIEW); Phase 6/7/9 verification results feed the inbox as signals (no second verification), a STRONG confirmation of a SUBMITTED/UNCERTAIN attempt verifies through `ExecutionService.verify` (`confirmation_email`); guarded lifecycle sync (→ INTERVIEWING, → REJECTED only); manual review (link / confirm / reject / confirm outcome / ignore / merge / reprocess, all audited, earlier rows superseded or retracted); `/api/v1/signals`, `/dashboard/signals` with the full trace signal → attribution → opportunity → candidate opportunity → preparation → execution → application history; no learning, no policy change from outcomes; 1,000-signal zero-AI benchmark. | **done 2026-09-17** |
| 11 | Outcome Learning Engine, volume-neutral (`app/learning`): a versioned learning dataset (`features-v1`) over executed applications joined to opportunity, company, normalized title, positioning role family, fit band and score, policy and gate versions, source, preparation lane / level / cover-letter mode / variant, execution method and verification, and the Phase 10 outcome events — time-aware (`as_of`: only attempts executed and events *observed* by then; explicit leakage tests), evidence-aware (labels only from events at or above the tenant's minimum evidence, the strongest evidence recorded per row, UNKNOWN / LIKELY never treated as VERIFIED); deterministic statistics (`beta-binomial+wilson-v1`: shrinkage toward the tenant's own baseline with a configurable prior strength, 95 % Wilson intervals, NONE / LOW / MEDIUM / HIGH confidence from `min_samples`); metrics per source / company / title / role family / fit band / lane / tailoring level / cover-letter mode / positioning variant / execution method (response, interview, rejection, assessment, verified-submission, uncertainty and review rates; median days to response and to rejection) plus job-side source discovery reliability; persistent, never-overwritten `learning_snapshots` / `learning_metrics` / `learning_recommendations` carrying learning, feature, smoothing, outcome-rules and attribution versions, window and generation time; recommendations phrased as observed associations with sample size, confidence, window and a non-causal caveat (sparse groups say "insufficient data"); an optional `expected_response` signal (response + interview rates vs baseline, confidence-weighted, neutral without data) that feeds the pre-existing `learned_prior` priority component only when `learning_settings.ordering_enabled` is on — order only, never admission, never a top-N, no policy / cap / band / threshold / blocklist / evidence mutation; `/api/v1/learning`, `/dashboard/learning`; zero AI; 1,000 / 5,000 / 10,000-application benchmarks. | **done 2026-09-18** |
| 12 | Hardening only, no new subsystem: concurrency audit with real thread races (queue claims, cap ledger, parallel signal delivery, parallel snapshots) and deterministic interleavings (two schedulers, two executors, Playwright vs extension, duplicate results / verifications); execution safety proven at the final gate (expired lease refused — new `LEASE_EXPIRED`, kill switch at every stage, blocklist / closure / stale preparation / changed artifact revalidated, UNKNOWN never resubmitted); startup recovery for runs a dead worker left RUNNING (`app/execution/recovery.py`, wired into the lifespan and `run_queue`); two real races fixed (`signals.observation_count` lost update → atomic increment; policy first-touch insert race → savepoint + re-read); one IDOR closed (legacy `/api/v5/applications` now tenant-scoped); extension message listeners now verify the sender; operational diagnostics (`/api/v1/ops/diagnostics`, `/dashboard/ops`); security regression group (IDOR sweep over every object family, write-route and dashboard-auth audits, traversal, hash validation, redaction, CORS, extension permissions and guards, single AI boundary, no string SQL, no secrets in config endpoints); migration chain round trips (fresh, −1, base) with constraint/index assertions; backup/restore test on a synthetic database; consolidated end-to-end soak; `docs/OPERATIONS.md` (invariants, recovery, health, backup, retention, indexes, SQLite/PostgreSQL parity, security summary). | **done 2026-09-19** |
| 13 | Real-world operational validation, no new subsystem: a re-runnable harness (`tools/validation/phase13_validate.py`) that drives the real pipeline against public Greenhouse / Lever / Ashby boards and application pages in dry run only (discovery → server-side mapping → document verification → pre-submit gate; `execute` never called on an employer page, nothing submitted), a compatibility matrix and raw observations under `docs/validation/phase13/`, and the reusable fixes the real world demanded: Lever's live payload shape (`hostedUrl` / `applyUrl`, form only on `/apply`), moved apply links followed (URLs in the raw hash, same-source refresh), client-rendered and iframe-embedded forms (`PLAYWRIGHT_SETTLE_MS`, child-frame discovery, the frame carried through fill / submit / rescan), listing / search / cookie-banner pages never mistaken for an application form, inline CAPTCHA widgets (Lever's hCaptcha) detected as solved-by-a-person through the provider's response token (never produced), upload controls mapped on name / id as well as label, attribution titles folded like the searched text; browser failure modes proven over loopback HTTP with route injection (slow, hung, 5xx, reset, tab closed mid-fill / after click, navigation and DOM changes mid-fill, late confirmation); realistic Signal Inbox messages, learning snapshot, data-chain audit, diagnostics, a short soak; the report separates PROVEN BY TESTS / OBSERVED / NOT YET PROVEN (live submission, the extension inside a real Chrome, multi-day operation). | **done 2026-09-12 (validation)** |

Existing Phase 4 (deterministic tailoring, `/api/v4`) and Phase 5/6-lite
(application engine, kill switch, `/api/v5`) code is retained and extended in
phases 6 and 8; nothing is deleted.

---

## 14. Target data model (minimum)

Tenant: `candidate_profiles`, `evidence` (raw documents), `evidence_nodes`
(graded claims), `positioning_variants`, `answer_bank_entries`,
`eligibility_decisions`, `match_scores`, `priority_scores`,
`tailored_artifacts`, `applications`, `application_events`,
`application_answers`, `application_queue`, `execution_attempts`,
`verifications`, `signals`, `outcomes`, `audit_events`.
Shared: `companies`, `job_sources`, `raw_jobs`, `jobs` (normalised),
`job_versions`, `job_requirements`, `opportunities`.
System: `ai_requests`, `ai_cache`, `ai_budgets`, `ai_usage`, `ingestion_runs`,
`system_metrics`, `kill_switches`.
All with foreign keys, indexes, unique constraints, timestamps, version
identifiers and tenant isolation. Alembic owns the schema.
