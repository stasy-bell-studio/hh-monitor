from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # hh.ru OAuth
    hh_client_id: str | None = None
    hh_client_secret: str | None = None
    hh_redirect_uri: str = "https://localhost:8080/callback"
    hh_user_agent: str = "SK21Vek HR Monitor (luk44646@gmail.com)"

    # hh.ru employer context (optional — inferred from OAuth token, but
    # passing explicitly is safe and required for some employer-scoped searches)
    hh_employer_id: str | None = None

    # Database
    database_url: str = "postgresql+asyncpg://hh_monitor:hh_monitor_dev@localhost:5432/hh_monitor"
    test_database_url: str | None = None  # set in .env for test isolation; prod ignores this

    # OpenRouter / LLM
    openrouter_api_key: str | None = None
    openrouter_model: str = "deepseek/deepseek-chat-v3-0324"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_http_referer: str = "https://github.com/Sam44ik/hh-monitor"
    openrouter_title: str = "hh-monitor"

    llm_prompt_version: str = "v2"
    # Candidates with fit_score below this threshold are not sent to LLM
    score_fit_min_for_llm: int = Field(default=60, ge=0, le=100)

    # Telegram
    telegram_bot_token: str | None = None
    telegram_hr_chat_id: str | None = None

    # Runtime
    env: str = "local"
    log_level: str = "INFO"


settings = Settings()
