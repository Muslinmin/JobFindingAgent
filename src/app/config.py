from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_tailoring_template_path() -> str:
    """Absolute, resolved from this file's own location rather than a bare
    relative string — the template is a bundled source asset under
    src/tailoring/templates/, not a user-facing working-directory path
    like profile_path/search_queries_path/tailoring_output_dir. A plain
    relative default breaks the moment the process's cwd isn't `src/`
    (e.g. running from the repo root, as pytest and most launchers do):
    Jinja2's FileSystemLoader resolves relative paths against cwd, not
    this file, so `"tailoring/templates/cv.tex.jinja"` silently pointed at
    a directory that doesn't exist from the repo root and raised
    TemplateNotFound — caught live by test_pipeline_live.py. Same pattern
    already used by tailoring/prompt.py's _DOMAIN_KNOWLEDGE_PATH and
    app/tailoring_usage_example.py's TEMPLATE_PATH."""
    return str(Path(__file__).resolve().parent.parent / "tailoring" / "templates" / "cv.tex.jinja")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    db_path:      str = "./jobs.db"
    profile_path: str = "profile.json"
    log_level:    str = "INFO"
    model: str = "gemini/gemini-2.0-flash-lite"
    model_api_key: str = ""

    # Scoring — embedding model, separate key since the provider may differ
    embedding_model: str = "text-embedding-3-small"
    embedding_api_key: str = ""

    # Scraper
    tavily_api_key: str = ""
    scrape_query: str = "software engineer Singapore"
    scrape_max_results: int = 10

    # Scraper — Careers@Gov adapter (OGP open-data mirror, scraper_layer.md WP-S1)
    careers_gov_data_url: str = (
        "https://raw.githubusercontent.com/opengovsg/careersgovsg-jobs-data/main/data/job-listings.json"
    )
    careers_gov_cache_ttl_s: int = 0

    # Telegram — two bots, one token each, one shared authorised chat_id
    telegram_chat_bot_token: str = ""
    telegram_notifications_bot_token: str = ""
    telegram_chat_id: int = 0

    # Internal API
    api_base_url: str = "http://localhost:8000"

    # Scheduling (scheduling_v2.md § Settings) — daily job hours, weekly digest day
    scrape_hour: int = 2
    lifecycle_hour: int = 3
    followup_hour: int = 4
    tailor_hour: int = 5
    digest_day: str = "mon"

    # Scheduling — pipeline throttles and time-rule thresholds
    tailor_batch_size: int = 10
    score_threshold: int = 5000
    follow_up_after_days: int = 7
    pending_expiry_days: int = 14
    stale_after_days: int = 14
    ghost_after_days: int = 35

    # Scheduling — file paths and misc job settings
    search_queries_path: str = "search_queries.json"
    adapter_delay_s: float = 1.0
    tailoring_template_path: str = Field(default_factory=_default_tailoring_template_path)
    tailoring_output_dir: str = "artifacts"
    digest_narrative: bool = False


settings = Settings()
