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
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Security. Single-user product: one shared key guards writes and the
    # dashboard. Unset is tolerated only in development, and logged loudly.
    api_key: Optional[str] = None

    # Database
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'careeros.db'}"

    # Career data (Phase 1)
    career_data_path: str = str(PROJECT_ROOT / "data" / "career_seed.json")

    # Firecrawl (Phase 2)
    firecrawl_api_key: Optional[str] = None
    firecrawl_api_url: str = "https://api.firecrawl.dev/v1/scrape"
    firecrawl_rate_limit: int = 5  # requests per minute

    # LLM extraction fallback (Phase 2)
    gemini_api_key: Optional[str] = None
    gemini_model: str = "gemini-2.0-flash"
    llm_provider: str = "stub"  # "gemini" | "stub"

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

    # Matching engine (Phase 3)
    match_policy_version: str = "v1"
    # Optional CPU-only semantic similarity. Requires the `semantic` extra;
    # the deterministic matcher works unchanged when this is off.
    semantic_matching_enabled: bool = False
    semantic_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    semantic_similarity_threshold: float = 0.72


settings = Settings()
