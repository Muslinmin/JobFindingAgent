from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    db_path:      str = "./jobs.db"
    profile_path: str = "profile.json"
    log_level:    str = "INFO"
    model: str = "gemini/gemini-2.0-flash-lite"
    model_api_key: str = ""

    # Scraper
    tavily_api_key: str = ""
    scrape_query: str = "software engineer Singapore"
    scrape_max_results: int = 10

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: int = 0

    # Internal API
    api_base_url: str = "http://localhost:8000"


settings = Settings()
