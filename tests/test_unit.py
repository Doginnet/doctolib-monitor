"""Offline unit tests validating Pydantic v2 models, SQLite WAL lifecycle, and Sniper/Mock Booking."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.alerts.dispatcher import AlertDispatcher
from src.booking.automator import DryRunBookingAutomator
from src.models.availability import AvailabilityPayload, ComputedAvailability
from src.models.booking import SniperRule
from src.models.profile import EnrichedDoctorProfile
from src.models.search import HcpSearchResponse, SeoSpecialtyLink
from src.pipelines.export import export_global_summary
from src.pipelines.storage import DoctolibStorage


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.mark.unit
def test_seo_specialty_links_parsing() -> None:
    raw = json.loads((FIXTURES_DIR / "seo_specialties_sample.json").read_text(encoding="utf-8"))
    munich_links = [SeoSpecialtyLink.model_validate(item) for item in raw["München"]]
    assert len(munich_links) == 3
    assert str(munich_links[0].url) == "https://www.doctolib.de/augenheilkunde/muenchen"
    assert munich_links[0].specialty_slug == "augenheilkunde"
    assert munich_links[0].city_slug == "muenchen"


@pytest.mark.unit
def test_hcp_search_response_normalization_and_nullable_sector() -> None:
    raw = json.loads((FIXTURES_DIR / "hcp_search_sample.json").read_text(encoding="utf-8"))
    # Simulate Doctolib unsplit insuranceSector={"split": False, "type": None} on second card
    raw["healthcareProviders"][1]["matchedVisitMotive"]["insuranceSector"] = {
        "split": False,
        "type": None,
    }
    parsed = HcpSearchResponse.model_validate(raw)

    assert parsed.total == 18
    assert len(parsed.healthcare_providers) == 2

    doc = parsed.healthcare_providers[0]
    assert doc.profile_id == 765796
    assert doc.practice_id == 664422
    assert doc.profile_slug == "ramona-rus"
    assert doc.full_display_name == "Dr. med. Ramona Rus"
    assert (
        str(doc.profile_url)
        == "https://www.doctolib.de/kardiologie/berlin/ramona-rus?pid=practice-664422"
    )
    assert (
        str(doc.avatar_url)
        == "https://media.doctolib.com/image/upload/q_auto:eco,f_auto/jdgzvdcdadx39rtibm69.jpg"
    )
    assert doc.is_bookable is True
    assert doc.effective_agenda_ids == [2176252]

    org = parsed.healthcare_providers[1]
    assert org.full_display_name == "CT-MRT Institut Berlin"
    assert org.entity_type == "ORGANIZATION"
    assert org.is_telehealth is True
    assert org.matched_visit_motive is not None
    assert org.matched_visit_motive.insurance_sector is not None
    assert org.matched_visit_motive.insurance_sector.normalized_sector == ""


@pytest.mark.unit
def test_availability_metrics_open_slots_and_fallbacks() -> None:
    raw = json.loads((FIXTURES_DIR / "availabilities_sample.json").read_text(encoding="utf-8"))
    payload = AvailabilityPayload.model_validate(raw)
    ref_time = datetime(2026, 10, 30, 13, 30, tzinfo=timezone.utc)

    metrics = payload.compute_metrics(
        profile_id=765796,
        practice_id=664422,
        visit_motive_id=13898103,
        motive_name="Erstuntersuchung Neupatient:in",
        insurance_sector="PRIVATE",
        reference_time=ref_time,
    )
    assert metrics.open_slots_count == 5
    assert metrics.next_slot_iso == "2026-10-31T14:30:00.000+01:00"
    assert metrics.wait_time_days == 1.0
    assert metrics.insurance_sector == "private"

    distant_payload = AvailabilityPayload.model_validate(
        {
            "availabilities": [{"date": "2026-10-30", "slots": []}],
            "total": 0,
            "next_slot": "2026-11-29T13:30:00.000+00:00",
        }
    )
    distant_metrics = distant_payload.compute_metrics(
        profile_id=101,
        practice_id=202,
        visit_motive_id=303,
        motive_name="Sprechstunde",
        insurance_sector="public",
        reference_time=ref_time,
    )
    assert distant_metrics.open_slots_count == 0
    assert distant_metrics.wait_time_days == 30.0


@pytest.mark.unit
def test_enriched_doctor_profile_parsing() -> None:
    raw = json.loads((FIXTURES_DIR / "doctor_profile_sample.json").read_text(encoding="utf-8"))
    profile = EnrichedDoctorProfile.from_api_response(raw)

    assert profile.profile_id == 1226421
    assert profile.slug == "mithal-nassar-berlin"
    assert profile.name_with_title == "Frau Dr. med. Mithal Nassar"
    assert profile.primary_phone == "030 76725289"
    assert "MRT Kardio / Herz" in profile.procedures
    assert profile.practices[0].opening_hours[0] == "Mo: 10:00-13:00, 14:00-18:00"
    assert "U Kottbusser Tor (U1/U8)" in profile.practices[0].transit_stations


@pytest.mark.unit
def test_sqlite_wal_deduplication_and_soft_deactivation(tmp_path: Path) -> None:
    db_file = tmp_path / "test_doctolib.db"
    storage = DoctolibStorage(db_file)

    raw_search = json.loads((FIXTURES_DIR / "hcp_search_sample.json").read_text(encoding="utf-8"))
    parsed = HcpSearchResponse.model_validate(raw_search)
    doc_a, doc_b = parsed.healthcare_providers[0], parsed.healthcare_providers[1]

    t1 = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
    storage.upsert_provider(doc_a, "berlin", "ct-kardio-herz", "private", seen_at=t1)
    storage.upsert_provider(doc_b, "berlin", "ct-kardio-herz", "private", seen_at=t1)

    # Insert two snapshots for doc_a to test Window CTE deduplication (latest wins)
    storage.insert_slot_snapshot(
        ComputedAvailability(
            profile_id=doc_a.profile_id,
            practice_id=doc_a.practice_id,
            visit_motive_id=13898103,
            motive_name="Ersttermin",
            insurance_sector="private",
            next_slot_iso="2026-11-15T10:00:00+01:00",
            wait_time_days=16.0,
            open_slots_count=1,
            scraped_at=t1,
        )
    )
    t2 = t1 + timedelta(hours=1)
    storage.insert_slot_snapshot(
        ComputedAvailability(
            profile_id=doc_a.profile_id,
            practice_id=doc_a.practice_id,
            visit_motive_id=13898103,
            motive_name="Ersttermin",
            insurance_sector="private",
            next_slot_iso="2026-10-31T14:30:00+01:00",
            wait_time_days=1.2,
            open_slots_count=4,
            scraped_at=t2,
        )
    )

    latest = storage.get_latest_availability_records(city_slug="berlin", active_only=True)
    assert len(latest) == 1
    assert latest[0]["wait_time_days"] == 1.2
    assert latest[0]["open_slots_count"] == 4

    # Simulate second sweep at t3 where ONLY doc_a is returned; doc_b should be soft-deactivated
    t3 = t1 + timedelta(hours=2)
    storage.upsert_provider(doc_a, "berlin", "ct-kardio-herz", "private", seen_at=t3)
    deactivated = storage.deactivate_unseen_providers(
        "berlin", "ct-kardio-herz", "private", sweep_start_time=t3
    )
    assert deactivated == 1

    active_providers = storage.get_all_providers(active_only=True)
    all_providers = storage.get_all_providers(active_only=False)
    assert len(active_providers) == 1
    assert len(all_providers) == 2  # Soft-deactivated, never physically deleted!


@pytest.mark.unit
def test_sniper_dry_run_booking_and_mock_dispatcher(tmp_path: Path) -> None:
    storage = DoctolibStorage(tmp_path / "sniper.db")
    raw_search = json.loads((FIXTURES_DIR / "hcp_search_sample.json").read_text(encoding="utf-8"))
    doc = HcpSearchResponse.model_validate(raw_search).healthcare_providers[0]
    storage.upsert_provider(doc, "berlin", "kardiologie", "private")

    avail = ComputedAvailability(
        profile_id=doc.profile_id,
        practice_id=doc.practice_id,
        visit_motive_id=13898103,
        motive_name="Erstuntersuchung Neupatient:in",
        insurance_sector="private",
        allow_new_patients=True,
        next_slot_iso="2026-10-31T14:30:00.000+01:00",
        wait_time_days=1.0,
        open_slots_count=3,
        sample_slots=["2026-10-31T14:30:00.000+01:00"],
    )
    storage.insert_slot_snapshot(avail)

    rule = SniperRule(
        max_wait_days=5.0,
        insurance_sector="private",
        allow_new_patients_only=True,
        preferred_hour_start=9,
        preferred_hour_end=17,
    )
    automator = DryRunBookingAutomator(rule=rule, storage=storage, extractor=None)
    intent = automator.evaluate_and_reserve_mock(doc, avail, enrich_profile=False)

    assert intent is not None
    assert intent.mode == "DRY_RUN_MOCK"
    assert intent.target_slot_iso == "2026-10-31T14:30:00.000+01:00"

    saved_intents = storage.get_booking_intents()
    assert len(saved_intents) == 1
    assert saved_intents[0]["status"] == "MOCK_RESERVED"

    # Safe Mock Mode Dispatcher (empty credentials -> never crashes)
    dispatcher = AlertDispatcher(
        webhook_url="",
        slack_webhook_url="",
        telegram_bot_token="",
        telegram_chat_id="",
    )
    assert dispatcher.is_mock_mode is True
    delivery = dispatcher.dispatch(intent)
    assert delivery == {"mock": "SIMULATED_OK"}

    exported = export_global_summary(storage, tmp_path / "samples", sniper_max_days=5.0)
    assert exported["urgent_csv"].exists()
    assert exported["booking_intents_json"].exists()
