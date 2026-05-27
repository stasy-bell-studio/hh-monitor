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

    llm_prompt_version: str = "v3"
    # Candidates with fit_score below this threshold are not sent to LLM
    score_fit_min_for_llm: int = Field(default=40, ge=0, le=100)

    # Telegram — bot credentials and targeting
    telegram_bot_token: str | None = None
    telegram_hr_group_id: int = 0  # negative int for supergroup, e.g. -1001234567890
    # comma-separated Telegram user IDs with admin privileges (e.g. "123456,789012")
    telegram_admin_user_ids: str = ""
    telegram_score_threshold: int = Field(default=60, ge=0, le=100)

    # Topic IDs for supergroup routing (0 = don't use topics)
    telegram_cards_topic_id: int = 0
    telegram_digest_topic_id: int = 0
    telegram_admin_topic_id: int = 0

    # Weekly digest schedule (used as reference; actual scheduling via systemd timer in session 7)
    weekly_digest_cron: str = "0 15 * * 5"  # Friday 15:00
    weekly_digest_tz: str = "Europe/Moscow"

    @property
    def admin_user_ids(self) -> list[int]:
        if not self.telegram_admin_user_ids:
            return []
        return [int(x.strip()) for x in self.telegram_admin_user_ids.split(",") if x.strip()]

    # Runtime
    env: str = "local"
    log_level: str = "INFO"


settings = Settings()
