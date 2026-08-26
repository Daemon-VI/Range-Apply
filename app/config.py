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
    debug: bool = True
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Database
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'careeros.db'}"

    # Career data (Phase 1)
    career_data_path: str = str(PROJECT_ROOT / "data" / "career_seed.json")

    # Firecrawl (Phase 2)
    firecrawl_api_key: Optional[str] = None
    firecrawl_rate_limit: int = 5  # requests per minute

    # LLM extraction fallback (Phase 2)
    gemini_api_key: Optional[str] = None
    llm_provider: str = "stub"  # "gemini" | "stub"

    # Discovery pipeline (Phase 2)
    default_discovery_concurrency: int = 3
    max_retries: int = 3
    retry_backoff_base: float = 2.0


settings = Settings()
