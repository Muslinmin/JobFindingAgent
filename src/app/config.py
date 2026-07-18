from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Scraper — Careers@Gov adapter (Algolia index behind jobs.careers.gov.sg)
    careers_gov_app_id: str = ""
    careers_gov_api_key: str = ""
    careers_gov_index: str = "job_index"
    careers_gov_hits_per_page: int = 20

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
    score_threshold: int = 7000
    follow_up_after_days: int = 7
    pending_expiry_days: int = 14
    stale_after_days: int = 14
    ghost_after_days: int = 35

    # Scheduling — file paths and misc job settings
    search_queries_path: str = "search_queries.json"
    adapter_delay_s: float = 1.0
    tailoring_template_path: str = "tailoring/templates/cv.tex.jinja"
    tailoring_output_dir: str = "artifacts"
    digest_narrative: bool = False


settings = Settings()
