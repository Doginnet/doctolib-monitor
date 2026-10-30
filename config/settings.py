"""Application settings powered by Pydantic BaseSettings."""

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

    # Doctolib Network & Anti-Bot Settings
    doctolib_base_url: str = Field(default="https://www.doctolib.de")
    doctolib_impersonate: str = Field(default="chrome124")
    doctolib_request_timeout: int = Field(default=25, ge=5, le=120)
    doctolib_min_delay: float = Field(default=0.35, ge=0.0, le=10.0)
    doctolib_max_delay: float = Field(default=0.85, ge=0.0, le=20.0)
    doctolib_max_retries: int = Field(default=3, ge=1, le=10)
    doctolib_circuit_breaker_threshold: int = Field(default=5, ge=2, le=20)

    # Storage & Export Paths
    doctolib_db_path: Path = Field(default=PROJECT_ROOT / "data" / "doctolib.db")
    doctolib_samples_dir: Path = Field(default=PROJECT_ROOT / "data" / "samples")
    doctolib_raw_dir: Path = Field(default=PROJECT_ROOT / "data" / "raw")
    doctolib_logs_dir: Path = Field(default=PROJECT_ROOT / "logs")

    # Appointment Sniper & Mock Auto-Booking Thresholds
    sniper_max_wait_days: float = Field(default=7.0, ge=0.0, le=365.0)
    sniper_allow_new_patients_only: bool = Field(default=True)
    sniper_preferred_hour_start: int = Field(default=7, ge=0, le=23)
    sniper_preferred_hour_end: int = Field(default=20, ge=0, le=23)

    # Multi-Channel Webhooks & Alerting (Empty -> Safe Mock / Simulation Mode)
    webhook_url: str = Field(default="")
    slack_webhook_url: str = Field(default="")
    telegram_bot_token: str = Field(default="")
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
