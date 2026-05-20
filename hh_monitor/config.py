from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # hh.ru OAuth
    hh_client_id: str | None = None
    hh_client_secret: str | None = None
    hh_redirect_uri: str = "https://localhost:8080/callback"
    hh_user_agent: str = "SK21Vek HR Monitor (luk44646@gmail.com)"

    # Database
    database_url: str = "postgresql+asyncpg://hh_monitor:hh_monitor_dev@localhost:5432/hh_monitor"

    # Notion
    notion_api_token: str | None = None
    notion_database_resumes_id: str | None = None
    notion_database_employees_id: str | None = None

    # Telegram
    telegram_bot_token: str | None = None
    telegram_hr_chat_id: str | None = None

    # Runtime
    env: str = "local"
    log_level: str = "INFO"


settings = Settings()
