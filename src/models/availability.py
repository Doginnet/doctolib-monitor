"""Pydantic v2 schemas and wait-time calculations for Doctolib /availabilities.json."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AvailabilityDay(BaseModel):
    model_config = ConfigDict(extra="ignore")

    date: str
    slots: list[str] = Field(default_factory=list)

    @field_validator("slots", mode="before")
    @classmethod
    def _coerce_slots(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        extracted: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                extracted.append(item.strip())
            elif isinstance(item, dict):
                start_val = item.get("start_date") or item.get("start")
                if isinstance(start_val, str) and start_val.strip():
                    extracted.append(start_val.strip())
        return extracted


class AvailabilityPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    availabilities: list[AvailabilityDay] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    next_slot: str | None = None
    reason: str | None = None
    message: str | None = None

    def extract_all_slots(self) -> list[str]:
        slots: list[str] = []
        for day in self.availabilities:
            slots.extend(day.slots)
        return sorted(slots)

    def compute_metrics(
        self,
        profile_id: int,
        practice_id: int,
        visit_motive_id: int,
        motive_name: str,
        insurance_sector: str,
        allow_new_patients: bool = True,
        reference_time: datetime | None = None,
    ) -> ComputedAvailability:
        now_utc = (reference_time or datetime.now(timezone.utc)).astimezone(timezone.utc)
        window_slots = self.extract_all_slots()

        earliest_iso: str | None = window_slots[0] if window_slots else self.next_slot
        wait_days: float | None = None

        if earliest_iso:
            try:
                parsed_slot = datetime.fromisoformat(earliest_iso).astimezone(timezone.utc)
                delta_seconds = (parsed_slot - now_utc).total_seconds()
                wait_days = round(max(0.0, delta_seconds / 86400.0), 2)
            except ValueError:
                wait_days = None

        return ComputedAvailability(
            profile_id=profile_id,
            practice_id=practice_id,
            visit_motive_id=visit_motive_id,
            motive_name=motive_name,
            insurance_sector=insurance_sector.lower(),
            allow_new_patients=allow_new_patients,
            next_slot_iso=earliest_iso,
            wait_time_days=wait_days,
            open_slots_count=len(window_slots),
            sample_slots=window_slots[:10],
            unavailability_reason=self.reason,
            scraped_at=now_utc,
        )


class ComputedAvailability(BaseModel):
    model_config = ConfigDict(extra="ignore")

    profile_id: int
    practice_id: int
    visit_motive_id: int
    motive_name: str = ""
    insurance_sector: str
    allow_new_patients: bool = True
    next_slot_iso: str | None = None
    wait_time_days: float | None = None
    open_slots_count: int = 0
    sample_slots: list[str] = Field(default_factory=list)
    unavailability_reason: str | None = None
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
