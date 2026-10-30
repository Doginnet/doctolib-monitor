"""Pydantic v2 contracts for Appointment Sniper rules and Pluggable Booking Automation."""

from __future__ import annotations

from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class SniperRule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_wait_days: float = Field(default=7.0, ge=0.0)
    insurance_sector: str | None = None
    allow_new_patients_only: bool = True
    preferred_hour_start: int = Field(default=7, ge=0, le=23)
    preferred_hour_end: int = Field(default=20, ge=0, le=23)
    require_telehealth: bool = False


class BookingIntentPayload(BaseModel):
    """Standardized event payload emitted by DryRunBookingAutomator for n8n/Slack/Telegram."""

    model_config = ConfigDict(extra="ignore")

    event_type: str = "DOCTOLIB_URGENT_SLOT_MATCH"
    mode: str = "DRY_RUN_MOCK"
    profile_id: int
    practice_id: int
    visit_motive_id: int
    agenda_ids: list[int] = Field(default_factory=list)
    doctor_name: str
    specialty: str
    city: str
    address: str
    clinic_phone: str | None = None
    opening_hours: list[str] = Field(default_factory=list)
    insurance_sector: str
    target_slot_iso: str
    wait_time_days: float
    booking_url: HttpUrl
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
