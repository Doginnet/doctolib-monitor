"""Multi-format sample exporter generating clean CSV & JSON slices per city/specialty and global summaries."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from loguru import logger

from src.pipelines.storage import DoctolibStorage


CSV_COLUMNS = [
    "profile_id",
    "practice_id",
    "full_name",
    "specialty_name",
    "city_name",
    "insurance_sector",
    "wait_time_days",
    "next_slot_iso",
    "open_slots_count",
    "allow_new_patients",
    "telehealth",
    "landline_number",
    "street_address",
    "zipcode",
    "latitude",
    "longitude",
    "profile_url",
    "is_active",
    "scraped_at",
]


def _write_csv(file_path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_json(file_path: Path, data: Any) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def export_scope_samples(
    storage: DoctolibStorage,
    samples_dir: Path,
    city_slug: str,
    specialty_slug: str,
    sniper_max_days: float = 7.0,
) -> dict[str, Path]:
    scope_dir = samples_dir / f"{city_slug}_{specialty_slug}"
    all_records = storage.get_latest_availability_records(
        city_slug=city_slug,
        specialty_slug=specialty_slug,
        active_only=True,
        limit=2000,
    )
    urgent_records = [
        r
        for r in all_records
        if r.get("wait_time_days") is not None and r["wait_time_days"] <= sniper_max_days
    ]

    paths = {
        "availability_csv": scope_dir / "availability.csv",
        "availability_json": scope_dir / "availability.json",
        "sniper_csv": scope_dir / "urgent_slots.csv",
        "sniper_json": scope_dir / "urgent_slots.json",
    }
    _write_csv(paths["availability_csv"], all_records, CSV_COLUMNS)
    _write_json(paths["availability_json"], all_records)
    _write_csv(paths["sniper_csv"], urgent_records, CSV_COLUMNS)
    _write_json(paths["sniper_json"], urgent_records)
    return paths


def export_global_summary(
    storage: DoctolibStorage,
    samples_dir: Path,
    sniper_max_days: float = 7.0,
) -> dict[str, Path]:
    summary_dir = samples_dir / "summary"
    all_records = storage.get_latest_availability_records(active_only=True, limit=5000)
    urgent_records = storage.get_latest_availability_records(
        active_only=True,
        max_wait_days=sniper_max_days,
        limit=1000,
    )
    gap_summary = storage.get_insurance_gap_summary(active_only=True)
    booking_intents = storage.get_booking_intents(limit=200)

    paths = {
        "all_csv": summary_dir / "all_availability.csv",
        "all_json": summary_dir / "all_availability.json",
        "urgent_csv": summary_dir / "urgent_slots.csv",
        "urgent_json": summary_dir / "urgent_slots.json",
        "insurance_gap_json": summary_dir / "insurance_gap_analytics.json",
        "booking_intents_json": summary_dir / "mock_booking_intents.json",
    }
    _write_csv(paths["all_csv"], all_records, CSV_COLUMNS)
    _write_json(paths["all_json"], all_records)
    _write_csv(paths["urgent_csv"], urgent_records, CSV_COLUMNS)
    _write_json(paths["urgent_json"], urgent_records)
    _write_json(paths["insurance_gap_json"], gap_summary)
    _write_json(paths["booking_intents_json"], booking_intents)

    logger.info(
        f"Exported global summary to {summary_dir} "
        f"({len(all_records)} records, {len(urgent_records)} urgent sniper slots)"
    )
    return paths
