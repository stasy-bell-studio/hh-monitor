from __future__ import annotations

from hh_monitor.config import Settings


def send_enabled(cfg: Settings) -> bool:
    """Return True if proactive Telegram sends are allowed.

    Priority: explicit TELEGRAM_SEND_ENABLED env var > env == "production".
    """
    if cfg.telegram_send_enabled is not None:
        return cfg.telegram_send_enabled
    return str(cfg.env).strip().lower() == "production"
