"""Application configuration."""

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    app_name: str = "CareerOS"
    app_env: str = "development"
    # Off by default: DEBUG leaks internals through error responses and echoes SQL.
    debug: bool = False
    log_level: str = "INFO"
    # Loopback by default: the API and dashboard hold personal data. The
    # desktop shell always binds 127.0.0.1 whatever this says.
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # "solo": everything on the candidate's own machine (SQLite or local
    # Postgres, headed Playwright, no auth beyond API_KEY). "hosted": the
    # free-tier topology (Supabase/Render/GitHub Actions, extension executor).
    # Both run the same code; the mode only changes defaults and warnings.
    deployment_mode: str = "solo"

    # Security. Single-user product: one shared key guards writes and the
    # dashboard. Unset is tolerated only in development, and logged loudly.
    api_key: Optional[str] = None

    # Tenant isolation. Every candidate-side row carries a tenant_id (blueprint
    # §11.11). Solo mode has exactly one tenant; hosted mode may have several.
    default_tenant_id: str = "default"

    # Database
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'careeros.db'}"
    # Connection pool, PostgreSQL only (SQLite ignores these). Supabase's free
    # tier caps direct connections low, so keep pool_size + max_overflow small
    # and rely on pre-ping to survive the pooler dropping idle connections.
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_pre_ping: bool = True
    db_pool_recycle_seconds: int = 1800

    # Career data (Phase 1)
    career_data_path: str = str(PROJECT_ROOT / "data" / "career_seed.json")

    # Firecrawl (Phase 2)
    firecrawl_api_key: Optional[str] = None
    firecrawl_api_url: str = "https://api.firecrawl.dev/v1/scrape"
    firecrawl_rate_limit: int = 5  # requests per minute

    # LLM extraction fallback (Phase 2). ``llm_provider`` is a legacy alias for
    # ``ai_provider``; AI never runs unless ``ai_enabled`` is true.
    gemini_api_key: Optional[str] = None
    gemini_model: str = "gemini-2.0-flash"
    llm_provider: str = "stub"  # "gemini" | "stub"

    # AI Gateway (Blueprint Phase 8b, §7). Off by default: every caller has a
    # deterministic path and the whole pipeline runs with zero AI calls.
    ai_enabled: bool = False
    #: AI for job-posting field extraction during discovery. Off by default even when
    #: AI is on: one call per posting exhausted a free-tier quota (HTTP 429) and held
    #: SQLite write locks while waiting. Answers and cover letters still use AI.
    ai_job_extraction_enabled: bool = False
    ai_provider: str = "stub"  # "stub" | "gemini" | "ollama"
    ai_model: Optional[str] = None
    ai_timeout_seconds: float = 20.0
    # Provider calls allowed per scope (a discovery run, one preparation).
    ai_max_calls_per_run: int = 50
    ai_max_output_tokens: int = 1024
    ai_cache_enabled: bool = True
    # Local directory of JSON entries; unset = in-memory (per process).
    ai_cache_dir: Optional[str] = str(PROJECT_ROOT / ".cache" / "ai")
    # Providers that may see candidate-side text (blueprint §7 PII class).
    # Local providers are always allowed; hosted ones must be listed here.
    ai_candidate_data_providers: str = "ollama"
    ai_ollama_url: str = "http://127.0.0.1:11434"
    ai_ollama_model: str = "llama3.2"
    # Durable per-call accounting rows (``ai_usage``); metadata only.
    ai_usage_persist: bool = True

    # Discovery pipeline (Phase 2)
    default_discovery_concurrency: int = 3
    max_retries: int = 3
    retry_backoff_base: float = 2.0
    # Per-source request budget, applied by the shared token-bucket limiter.
    source_rate_limit_per_minute: int = 30
    source_request_timeout: float = 15.0
    # A run still marked "running" after this long is treated as interrupted.
    stale_run_timeout_minutes: int = 60
    # Skip the stale-job sweep when a run failed this fraction of its jobs.
    closure_max_failure_ratio: float = 0.25
    # Blueprint Phase 3: politeness and resilience per source.
    # Random extra fraction added to each exponential backoff delay.
    retry_jitter_ratio: float = 0.25
    # Consecutive terminal failures before a source's circuit opens, and for how long.
    source_circuit_failure_threshold: int = 3
    source_circuit_open_seconds: float = 600.0
    # Default polling interval for a tracked board (source_health.next_poll_at).
    discovery_default_poll_minutes: int = 360
    # After a run, ensure candidate_opportunities rows (DISCOVERED) for these
    # tenants. Cheap inserts only; no eligibility/fit work during ingestion.
    discovery_project_tenants: str = "default"
    # Cap on error strings kept per run row (the rest are counted, not stored).
    discovery_max_errors_stored: int = 50

    # Execution foundation (Blueprint Phase 6). The MOCK executor never touches
    # the network; it is only registered for API use when this is on (tests,
    # local demos). Real browser executors arrive in later phases.
    execution_mock_enabled: bool = False
    # Retries per execution queue item before a human must look.
    execution_max_attempts: int = 3
    # Lease per execution claim; a crashed executor's item is reclaimable after it.
    execution_lease_seconds: int = 900
    # Local files the executor may upload for file fields (the candidate's own
    # rendered resume / cover letter). Phase 4 artifacts are text; nothing is
    # rendered automatically, so without these a required upload hands off.
    execution_resume_file: Optional[str] = None
    execution_cover_letter_file: Optional[str] = None

    # Local Playwright executor (Blueprint Phase 7). Runs on the candidate's
    # machine only; no cloud browser. Dry-run is the default: it navigates,
    # inspects and maps, and never presses submit until switched off.
    playwright_dry_run: bool = True
    playwright_headless: bool = True
    playwright_browser: str = "chromium"
    playwright_navigation_timeout_ms: int = 30000
    playwright_action_timeout_ms: int = 10000
    playwright_submit_wait_ms: int = 15000
    # After the load event, keep re-scanning for up to this long while a page
    # renders its form client-side (SPAs, embedded ATS iframes). Phase 13.
    playwright_settle_ms: int = 8000
    # Seconds to pause between navigations so employer sites are not hammered.
    playwright_min_delay_seconds: float = 2.0
    # Optional persistent browser profile (local directory). Lets an already
    # signed-in session be reused; never uploaded, never read by the server.
    playwright_profile_dir: Optional[str] = None
    # With a visible browser, wait this long for the person to clear a CAPTCHA /
    # login / MFA in the window before handing off. 0 = hand off immediately.
    playwright_handoff_wait_seconds: int = 0
    # Opt-in local directory for handoff screenshots (may contain candidate
    # data; local only, never uploaded). None = no screenshots.
    playwright_debug_artifacts_dir: Optional[str] = None
    # Local worker: one browser, one item at a time by default.
    execution_worker_concurrency: int = 1
    execution_worker_poll_seconds: float = 5.0

    # Desktop shell local state (notification cursor). Local files only, git-ignored.
    desktop_state_dir: str = str(PROJECT_ROOT / ".cache" / "desktop")

    # Document artifacts (Blueprint Phase 8): rendered locally from validated
    # preparations, stored under a local root (never in the DB, never uploaded
    # anywhere by the server). Paths are tenant/opportunity/preparation scoped.
    documents_root: str = str(PROJECT_ROOT / "data" / "documents")
    # Optional TrueType font for full Unicode coverage in PDFs; when unset a
    # common system font is used if found, else the built-in Helvetica.
    documents_font_path: Optional[str] = None
    documents_max_pages_resume: int = 4
    documents_max_pages_cover_letter: int = 2
    documents_max_bytes: int = 5 * 1024 * 1024
    # Optional fixed date line for cover letters (ISO date). Unset = no date
    # line, so rendering stays deterministic and never invents a date.
    documents_letter_date: Optional[str] = None

    # Signal Inbox (Blueprint Phase 10): only a bounded excerpt of a supplied
    # message is stored (credential-shaped fragments redacted), never raw HTML
    # or headers. Excerpts of settled signals older than the retention are
    # purged by ``SignalInboxService.purge_excerpts`` (hashes and outcomes stay).
    signal_excerpt_chars: int = 2000
    signal_excerpt_retention_days: int = 180

    # Matching engine (Phase 3)
    match_policy_version: str = "v1"
    # Optional CPU-only semantic similarity. Requires the `semantic` extra;
    # the deterministic matcher works unchanged when this is off.
    semantic_matching_enabled: bool = False
    semantic_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    semantic_similarity_threshold: float = 0.72


settings = Settings()
