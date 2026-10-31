"""SQLite WAL storage engine with soft-deactivation lifecycle and Window CTE analytics."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from config.cities import MEDICAL_SPECIALTIES, resolve_city
from src.models.availability import ComputedAvailability
from src.models.booking import BookingIntentPayload
from src.models.profile import EnrichedDoctorProfile
from src.models.search import HealthcareProviderCard


class DoctolibStorage:
    """Manages relational persistence in SQLite WAL mode without destructive deletes."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS practices (
                    practice_id INTEGER PRIMARY KEY,
                    clinic_name TEXT,
                    street_address TEXT,
                    zipcode TEXT,
                    city TEXT,
                    city_slug TEXT,
                    latitude REAL,
                    longitude REAL,
                    landline_number TEXT,
                    website_url TEXT,
                    opening_hours_json TEXT DEFAULT '[]',
                    transit_stations_json TEXT DEFAULT '[]',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS practitioners (
                    profile_id INTEGER NOT NULL,
                    practice_id INTEGER NOT NULL,
                    profile_slug TEXT NOT NULL,
                    full_name TEXT NOT NULL,
                    title TEXT,
                    entity_type TEXT,
                    specialty_slug TEXT,
                    specialty_name TEXT,
                    city_slug TEXT,
                    city_name TEXT,
                    languages_json TEXT DEFAULT '[]',
                    procedures_json TEXT DEFAULT '[]',
                    telehealth INTEGER DEFAULT 0,
                    profile_url TEXT NOT NULL,
                    avatar_url TEXT,
                    regulation_sector TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    is_active INTEGER DEFAULT 1,
                    PRIMARY KEY (profile_id, practice_id)
                );

                CREATE TABLE IF NOT EXISTS provider_search_scopes (
                    profile_id INTEGER NOT NULL,
                    practice_id INTEGER NOT NULL,
                    city_slug TEXT NOT NULL,
                    search_keyword TEXT NOT NULL,
                    insurance_sector TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    is_active INTEGER DEFAULT 1,
                    PRIMARY KEY (profile_id, practice_id, city_slug, search_keyword, insurance_sector)
                );

                CREATE TABLE IF NOT EXISTS slot_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id INTEGER NOT NULL,
                    practice_id INTEGER NOT NULL,
                    visit_motive_id INTEGER NOT NULL,
                    motive_name TEXT,
                    insurance_sector TEXT NOT NULL,
                    allow_new_patients INTEGER DEFAULT 1,
                    next_slot_iso TEXT,
                    wait_time_days REAL,
                    open_slots_count INTEGER DEFAULT 0,
                    sample_slots_json TEXT DEFAULT '[]',
                    unavailability_reason TEXT,
                    scraped_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_lookup
                    ON slot_snapshots(profile_id, practice_id, insurance_sector, id DESC);

                CREATE TABLE IF NOT EXISTS booking_intents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id INTEGER NOT NULL,
                    practice_id INTEGER NOT NULL,
                    doctor_name TEXT NOT NULL,
                    specialty TEXT,
                    city TEXT,
                    insurance_sector TEXT NOT NULL,
                    target_slot_iso TEXT NOT NULL,
                    wait_time_days REAL NOT NULL,
                    clinic_phone TEXT,
                    booking_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    sub_type TEXT NOT NULL,
                    city_slug TEXT,
                    city_name TEXT,
                    specialty_slug TEXT,
                    specialty_name TEXT,
                    doctor_slug TEXT,
                    doctor_name TEXT,
                    profile_id INTEGER,
                    practice_id INTEGER,
                    visit_motive_id INTEGER,
                    visit_motive_name TEXT,
                    agenda_ids_json TEXT DEFAULT '[]',
                    telehealth INTEGER DEFAULT 0,
                    clinic_address TEXT,
                    clinic_phone TEXT,
                    booking_url TEXT,
                    insurance_sector TEXT NOT NULL,
                    max_wait_days REAL NOT NULL DEFAULT 14.0,
                    hour_start INTEGER NOT NULL DEFAULT 7,
                    hour_end INTEGER NOT NULL DEFAULT 20,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_checked_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_subscriptions_active
                    ON subscriptions(is_active, chat_id);

                CREATE TABLE IF NOT EXISTS sent_slot_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    profile_id INTEGER NOT NULL,
                    practice_id INTEGER NOT NULL,
                    slot_iso TEXT NOT NULL,
                    sent_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sent_alerts_dedup
                    ON sent_slot_alerts(subscription_id, profile_id, practice_id, slot_iso);
                """
            )

    def upsert_provider(
        self,
        card: HealthcareProviderCard,
        city_key: str,
        search_keyword: str,
        insurance_sector: str,
        seen_at: datetime | None = None,
    ) -> None:
        now_iso = (seen_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        city_preset = resolve_city(city_key)
        spec_slug = card.speciality.slug or search_keyword
        spec_name = card.speciality.name or MEDICAL_SPECIALTIES.get(search_keyword, search_keyword)

        clinic_name = (
            card.full_display_name
            if card.entity_type == "ORGANIZATION"
            else f"Praxis {card.full_display_name}"
        )

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO practices (
                    practice_id, clinic_name, street_address, zipcode, city, city_slug,
                    latitude, longitude, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(practice_id) DO UPDATE SET
                    street_address = COALESCE(excluded.street_address, practices.street_address),
                    zipcode = COALESCE(excluded.zipcode, practices.zipcode),
                    city = COALESCE(excluded.city, practices.city),
                    city_slug = excluded.city_slug,
                    latitude = COALESCE(excluded.latitude, practices.latitude),
                    longitude = COALESCE(excluded.longitude, practices.longitude),
                    updated_at = excluded.updated_at
                """,
                (
                    card.practice_id,
                    clinic_name,
                    card.location.address,
                    card.location.zipcode,
                    card.location.city or city_preset.name,
                    city_preset.slug,
                    card.location.lat,
                    card.location.lng,
                    now_iso,
                ),
            )

            conn.execute(
                """
                INSERT INTO practitioners (
                    profile_id, practice_id, profile_slug, full_name, title, entity_type,
                    specialty_slug, specialty_name, city_slug, city_name, languages_json,
                    telehealth, profile_url, avatar_url, regulation_sector,
                    first_seen_at, last_seen_at, is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(profile_id, practice_id) DO UPDATE SET
                    profile_slug = excluded.profile_slug,
                    full_name = excluded.full_name,
                    title = COALESCE(excluded.title, practitioners.title),
                    entity_type = excluded.entity_type,
                    specialty_slug = excluded.specialty_slug,
                    specialty_name = excluded.specialty_name,
                    city_slug = excluded.city_slug,
                    city_name = excluded.city_name,
                    languages_json = excluded.languages_json,
                    telehealth = excluded.telehealth,
                    profile_url = excluded.profile_url,
                    avatar_url = COALESCE(excluded.avatar_url, practitioners.avatar_url),
                    regulation_sector = COALESCE(excluded.regulation_sector, practitioners.regulation_sector),
                    last_seen_at = excluded.last_seen_at,
                    is_active = 1
                """,
                (
                    card.profile_id,
                    card.practice_id,
                    card.profile_slug,
                    card.full_display_name,
                    card.title,
                    card.entity_type,
                    spec_slug,
                    spec_name,
                    city_preset.slug,
                    city_preset.name,
                    json.dumps(card.languages, ensure_ascii=False),
                    1 if card.is_telehealth else 0,
                    str(card.profile_url),
                    str(card.avatar_url) if card.avatar_url else None,
                    card.regulation_sector,
                    now_iso,
                    now_iso,
                ),
            )

            conn.execute(
                """
                INSERT INTO provider_search_scopes (
                    profile_id, practice_id, city_slug, search_keyword, insurance_sector,
                    last_seen_at, is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(profile_id, practice_id, city_slug, search_keyword, insurance_sector)
                DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    is_active = 1
                """,
                (
                    card.profile_id,
                    card.practice_id,
                    city_preset.slug,
                    search_keyword.strip().lower(),
                    insurance_sector.strip().lower(),
                    now_iso,
                ),
            )

    def apply_profile_enrichment(
        self,
        enriched: EnrichedDoctorProfile,
        fallback_practice_id: int | None = None,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            for prac in enriched.practices:
                target_pid = prac.practice_id or fallback_practice_id
                if not target_pid:
                    continue
                conn.execute(
                    """
                    UPDATE practices
                    SET clinic_name = CASE WHEN ? != '' THEN ? ELSE clinic_name END,
                        landline_number = COALESCE(?, landline_number),
                        website_url = COALESCE(?, website_url),
                        opening_hours_json = CASE WHEN ? != '[]' THEN ? ELSE opening_hours_json END,
                        transit_stations_json = CASE WHEN ? != '[]' THEN ? ELSE transit_stations_json END,
                        updated_at = ?
                    WHERE practice_id = ?
                    """,
                    (
                        prac.formal_name,
                        prac.formal_name,
                        prac.landline_number,
                        str(enriched.website_url) if enriched.website_url else None,
                        json.dumps(prac.opening_hours, ensure_ascii=False),
                        json.dumps(prac.opening_hours, ensure_ascii=False),
                        json.dumps(prac.transit_stations, ensure_ascii=False),
                        json.dumps(prac.transit_stations, ensure_ascii=False),
                        now_iso,
                        target_pid,
                    ),
                )

            if enriched.procedures:
                conn.execute(
                    """
                    UPDATE practitioners
                    SET procedures_json = ?
                    WHERE profile_slug = ? OR profile_id = ?
                    """,
                    (
                        json.dumps(enriched.procedures, ensure_ascii=False),
                        enriched.slug,
                        enriched.profile_id,
                    ),
                )

    def insert_slot_snapshot(self, avail: ComputedAvailability) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO slot_snapshots (
                    profile_id, practice_id, visit_motive_id, motive_name,
                    insurance_sector, allow_new_patients, next_slot_iso,
                    wait_time_days, open_slots_count, sample_slots_json,
                    unavailability_reason, scraped_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    avail.profile_id,
                    avail.practice_id,
                    avail.visit_motive_id,
                    avail.motive_name,
                    avail.insurance_sector.lower(),
                    1 if avail.allow_new_patients else 0,
                    avail.next_slot_iso,
                    avail.wait_time_days,
                    avail.open_slots_count,
                    json.dumps(avail.sample_slots, ensure_ascii=False),
                    avail.unavailability_reason,
                    avail.scraped_at.astimezone(timezone.utc).isoformat(),
                ),
            )

    def record_booking_intent(
        self,
        intent: BookingIntentPayload,
        status: str = "MOCK_RESERVED",
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO booking_intents (
                    profile_id, practice_id, doctor_name, specialty, city,
                    insurance_sector, target_slot_iso, wait_time_days,
                    clinic_phone, booking_url, status, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.profile_id,
                    intent.practice_id,
                    intent.doctor_name,
                    intent.specialty,
                    intent.city,
                    intent.insurance_sector,
                    intent.target_slot_iso,
                    intent.wait_time_days,
                    intent.clinic_phone,
                    str(intent.booking_url),
                    status,
                    intent.model_dump_json(),
                    intent.created_at.astimezone(timezone.utc).isoformat(),
                ),
            )
            return int(cursor.lastrowid or 0)

    def deactivate_unseen_providers(
        self,
        city_key: str,
        search_keyword: str,
        insurance_sector: str,
        sweep_start_time: datetime,
    ) -> int:
        """Soft-deactivates providers missing from a full directory sweep (never executes DELETE)."""
        city_slug = resolve_city(city_key).slug
        start_iso = sweep_start_time.astimezone(timezone.utc).isoformat()
        kw = search_keyword.strip().lower()
        sec = insurance_sector.strip().lower()

        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE provider_search_scopes
                SET is_active = 0
                WHERE city_slug = ?
                  AND search_keyword = ?
                  AND insurance_sector = ?
                  AND last_seen_at < ?
                  AND is_active = 1
                """,
                (city_slug, kw, sec, start_iso),
            )
            deactivated_count = cur.rowcount

            # Sync practitioner global is_active flag if inactive across all scopes
            conn.execute(
                """
                UPDATE practitioners
                SET is_active = 0
                WHERE is_active = 1
                  AND NOT EXISTS (
                      SELECT 1 FROM provider_search_scopes sc
                      WHERE sc.profile_id = practitioners.profile_id
                        AND sc.practice_id = practitioners.practice_id
                        AND sc.is_active = 1
                  )
                """
            )

        if deactivated_count > 0:
            logger.info(
                f"Lifecycle sync [{city_slug}/{kw}/{sec}]: soft-deactivated {deactivated_count} stale entries"
            )
        return deactivated_count

    def get_latest_availability_records(
        self,
        city_slug: str | None = None,
        specialty_slug: str | None = None,
        insurance_sector: str | None = None,
        max_wait_days: float | None = None,
        active_only: bool = True,
        bookable_only: bool = False,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Uses SQL Window CTE (ROW_NUMBER) to join latest slot snapshot per doctor & insurance sector."""
        conditions: list[str] = ["ls.rn = 1"]
        params: list[Any] = []

        if active_only:
            conditions.append("p.is_active = 1")
        if city_slug:
            conditions.append("p.city_slug = ?")
            params.append(city_slug.lower())
        if specialty_slug:
            conditions.append("p.specialty_slug = ?")
            params.append(specialty_slug.lower())
        if insurance_sector:
            conditions.append("ls.insurance_sector = ?")
            params.append(insurance_sector.lower())
        if bookable_only:
            conditions.append("ls.wait_time_days IS NOT NULL")
        if max_wait_days is not None:
            conditions.append("ls.wait_time_days IS NOT NULL AND ls.wait_time_days <= ?")
            params.append(max_wait_days)

        where_sql = " AND ".join(conditions)
        params.append(limit)

        query = f"""
            WITH latest_snapshots AS (
                SELECT
                    s.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY s.profile_id, s.practice_id, s.insurance_sector
                        ORDER BY s.id DESC
                    ) AS rn
                FROM slot_snapshots s
            )
            SELECT
                p.profile_id,
                p.practice_id,
                p.profile_slug,
                p.full_name,
                p.entity_type,
                p.specialty_slug,
                p.specialty_name,
                p.city_slug,
                p.city_name,
                p.languages_json,
                p.procedures_json,
                p.telehealth,
                p.profile_url,
                p.avatar_url,
                p.is_active,
                p.last_seen_at,
                pr.clinic_name,
                pr.street_address,
                pr.zipcode,
                pr.latitude,
                pr.longitude,
                pr.landline_number,
                pr.website_url,
                pr.opening_hours_json,
                ls.visit_motive_id,
                ls.motive_name,
                ls.insurance_sector,
                ls.allow_new_patients,
                ls.next_slot_iso,
                ls.wait_time_days,
                ls.open_slots_count,
                ls.sample_slots_json,
                ls.scraped_at
            FROM latest_snapshots ls
            JOIN practitioners p
                ON p.profile_id = ls.profile_id AND p.practice_id = ls.practice_id
            LEFT JOIN practices pr
                ON pr.practice_id = p.practice_id
            WHERE {where_sql}
            ORDER BY
                CASE WHEN ls.wait_time_days IS NULL THEN 1 ELSE 0 END ASC,
                ls.wait_time_days ASC,
                ls.open_slots_count DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_all_providers(self, active_only: bool = True) -> list[dict[str, Any]]:
        where = "WHERE p.is_active = 1" if active_only else ""
        query = f"""
            SELECT
                p.*,
                pr.clinic_name,
                pr.street_address,
                pr.zipcode,
                pr.latitude,
                pr.longitude,
                pr.landline_number,
                pr.website_url,
                pr.opening_hours_json
            FROM practitioners p
            LEFT JOIN practices pr ON pr.practice_id = p.practice_id
            {where}
            ORDER BY p.city_name ASC, p.specialty_name ASC, p.full_name ASC
        """
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(query).fetchall()]

    def get_insurance_gap_summary(self, active_only: bool = True) -> list[dict[str, Any]]:
        """Aggregates GKV (public) vs. PKV (private) wait-time metrics grouped by canonical specialty slug."""
        active_clause = "AND p.is_active = 1" if active_only else ""
        query = f"""
            WITH latest_snapshots AS (
                SELECT
                    s.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY s.profile_id, s.practice_id, s.insurance_sector
                        ORDER BY s.id DESC
                    ) AS rn
                FROM slot_snapshots s
            )
            SELECT
                p.city_name,
                COALESCE(sc.search_keyword, p.specialty_slug) AS canonical_specialty_slug,
                ls.insurance_sector,
                COUNT(*) AS total_checked,
                SUM(CASE WHEN ls.wait_time_days IS NOT NULL THEN 1 ELSE 0 END) AS with_open_slots,
                ROUND(AVG(ls.wait_time_days), 2) AS avg_wait_days,
                ROUND(MIN(ls.wait_time_days), 2) AS min_wait_days,
                ROUND(MAX(ls.wait_time_days), 2) AS max_wait_days
            FROM latest_snapshots ls
            JOIN practitioners p
                ON p.profile_id = ls.profile_id AND p.practice_id = ls.practice_id
            LEFT JOIN (
                SELECT profile_id, practice_id, MIN(search_keyword) AS search_keyword
                FROM provider_search_scopes
                GROUP BY profile_id, practice_id
            ) sc
                ON sc.profile_id = p.profile_id AND sc.practice_id = p.practice_id
            WHERE ls.rn = 1 {active_clause}
            GROUP BY p.city_name, canonical_specialty_slug, ls.insurance_sector
            ORDER BY p.city_name ASC, canonical_specialty_slug ASC, ls.insurance_sector ASC
        """
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(query).fetchall()]
            for row in rows:
                slug = row.get("canonical_specialty_slug") or ""
                row["specialty_name"] = MEDICAL_SPECIALTIES.get(slug, slug.title())
            return rows

    def get_booking_intents(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM booking_intents ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    # -------------------------------------------------------------------------
    # Telegram Bot Subscriptions & Deduplicated Slot Alerting
    # -------------------------------------------------------------------------

    def create_subscription(
        self,
        *,
        chat_id: int,
        sub_type: str,
        insurance_sector: str,
        max_wait_days: float,
        hour_start: int = 7,
        hour_end: int = 20,
        city_slug: str | None = None,
        city_name: str | None = None,
        specialty_slug: str | None = None,
        specialty_name: str | None = None,
        doctor_slug: str | None = None,
        doctor_name: str | None = None,
        profile_id: int | None = None,
        practice_id: int | None = None,
        visit_motive_id: int | None = None,
        visit_motive_name: str | None = None,
        agenda_ids: list[int] | None = None,
        telehealth: bool = False,
        clinic_address: str | None = None,
        clinic_phone: str | None = None,
        booking_url: str | None = None,
    ) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO subscriptions (
                    chat_id, sub_type, city_slug, city_name, specialty_slug, specialty_name,
                    doctor_slug, doctor_name, profile_id, practice_id, visit_motive_id,
                    visit_motive_name, agenda_ids_json, telehealth, clinic_address, clinic_phone,
                    booking_url, insurance_sector, max_wait_days, hour_start, hour_end,
                    is_active, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    chat_id,
                    sub_type,
                    city_slug,
                    city_name,
                    specialty_slug,
                    specialty_name,
                    doctor_slug,
                    doctor_name,
                    profile_id,
                    practice_id,
                    visit_motive_id,
                    visit_motive_name,
                    json.dumps(agenda_ids or []),
                    1 if telehealth else 0,
                    clinic_address,
                    clinic_phone,
                    booking_url,
                    insurance_sector.strip().lower(),
                    max_wait_days,
                    hour_start,
                    hour_end,
                    now_iso,
                ),
            )
            return int(cur.lastrowid or 0)

    def _deserialize_subscription(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["agenda_ids"] = json.loads(item.get("agenda_ids_json") or "[]")
        except json.JSONDecodeError:
            item["agenda_ids"] = []
        item["telehealth"] = bool(item.get("telehealth"))
        item["is_active"] = bool(item.get("is_active"))
        return item

    def get_subscription(self, sub_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
            return self._deserialize_subscription(row) if row else None

    def list_subscriptions(
        self,
        chat_id: int | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if active_only:
            clauses.append("is_active = 1")
        if chat_id is not None:
            clauses.append("chat_id = ?")
            params.append(chat_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM subscriptions {where_sql} ORDER BY id DESC",
                params,
            ).fetchall()
            return [self._deserialize_subscription(r) for r in rows]

    def set_subscription_active(
        self,
        sub_id: int,
        is_active: bool,
        chat_id: int | None = None,
    ) -> bool:
        """Soft-toggles subscription state (is_active = 0/1) without destructive DELETE."""
        params: list[Any] = [1 if is_active else 0, sub_id]
        chat_clause = ""
        if chat_id is not None:
            chat_clause = " AND chat_id = ?"
            params.append(chat_id)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE subscriptions SET is_active = ? WHERE id = ?{chat_clause}",
                params,
            )
            return cur.rowcount > 0

    def touch_subscription_checked(self, sub_id: int) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE subscriptions SET last_checked_at = ? WHERE id = ?",
                (now_iso, sub_id),
            )

    def was_slot_alert_sent(
        self,
        *,
        subscription_id: int,
        profile_id: int,
        practice_id: int,
        slot_iso: str,
        dedup_hours: float = 24.0,
        now: datetime | None = None,
    ) -> bool:
        cutoff = ((now or datetime.now(timezone.utc)) - timedelta(hours=dedup_hours)).astimezone(
            timezone.utc
        )
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT sent_at FROM sent_slot_alerts
                WHERE subscription_id = ? AND profile_id = ? AND practice_id = ? AND slot_iso = ?
                ORDER BY id DESC LIMIT 1
                """,
                (subscription_id, profile_id, practice_id, slot_iso),
            ).fetchone()
            if not row:
                return False
            if dedup_hours <= 0:
                return False
            try:
                sent_dt = datetime.fromisoformat(row["sent_at"]).astimezone(timezone.utc)
                return sent_dt >= cutoff
            except ValueError:
                return True

    def record_sent_slot_alert(
        self,
        *,
        subscription_id: int,
        chat_id: int,
        profile_id: int,
        practice_id: int,
        slot_iso: str,
        sent_at: datetime | None = None,
    ) -> None:
        ts = (sent_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sent_slot_alerts (
                    subscription_id, chat_id, profile_id, practice_id, slot_iso, sent_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (subscription_id, chat_id, profile_id, practice_id, slot_iso, ts),
            )
