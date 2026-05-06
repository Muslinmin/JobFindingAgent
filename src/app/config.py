from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    db_path: str = "./jobs.db"
    log_level: str = "INFO"
    model: str = "claude-sonnet-4-5"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""


settings = Settings()
