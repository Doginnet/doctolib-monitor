"""Intuitive application configuration powered by Pydantic BaseSettings (.env)."""

from __future__ import annotations

from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 1. Telegram Bot & Background Subscription Poller (Primary User Service)
    telegram_bot_token: str = Field(default="")
    bot_poll_interval_minutes: float = Field(default=10.0, ge=0.5, le=1440.0)
    bot_max_doctors_per_search: int = Field(default=15, ge=1, le=100)
    bot_alert_dedup_hours: float = Field(default=24.0, ge=0.0, le=720.0)
    bot_max_alerts_per_cycle: int = Field(default=3, ge=1, le=20)

    # 2. Default Slot Filtering Thresholds (CLI & Engineer Web Console)
    sniper_max_wait_days: float = Field(default=7.0, ge=0.0, le=365.0)
    sniper_allow_new_patients_only: bool = Field(default=True)
    sniper_preferred_hour_start: int = Field(default=7, ge=0, le=23)
    sniper_preferred_hour_end: int = Field(default=20, ge=0, le=23)

    # 3. Doctolib Network & Anti-Bot Settings (Under the Hood)
    doctolib_base_url: str = Field(default="https://www.doctolib.de")
    doctolib_impersonate: str = Field(default="chrome124")
    doctolib_request_timeout: int = Field(default=25, ge=5, le=120)
    doctolib_min_delay: float = Field(default=0.40, ge=0.0, le=10.0)
    doctolib_max_delay: float = Field(default=0.90, ge=0.0, le=20.0)
    doctolib_max_retries: int = Field(default=3, ge=1, le=10)
    doctolib_circuit_breaker_threshold: int = Field(default=5, ge=2, le=20)

    # 4. Storage Paths & Optional External Webhooks (n8n / Slack)
    doctolib_db_path: Path = Field(default=PROJECT_ROOT / "data" / "doctolib.db")
    doctolib_samples_dir: Path = Field(default=PROJECT_ROOT / "data" / "samples")
    doctolib_raw_dir: Path = Field(default=PROJECT_ROOT / "data" / "raw")
    doctolib_logs_dir: Path = Field(default=PROJECT_ROOT / "logs")

    webhook_url: str = Field(default="")
    slack_webhook_url: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    def resolved_db_path(self) -> Path:
        if self.doctolib_db_path.is_absolute():
            return self.doctolib_db_path
        return PROJECT_ROOT / self.doctolib_db_path

    def resolved_samples_dir(self) -> Path:
        if self.doctolib_samples_dir.is_absolute():
            return self.doctolib_samples_dir
        return PROJECT_ROOT / self.doctolib_samples_dir


settings = Settings()
