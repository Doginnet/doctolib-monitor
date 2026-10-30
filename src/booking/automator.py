"""Pluggable Dry-Run Booking Automator & Appointment Sniper rule evaluator."""

from __future__ import annotations

from datetime import datetime

from loguru import logger

from src.extractors.doctolib import DoctolibExtractor
from src.models.availability import ComputedAvailability
from src.models.booking import BookingIntentPayload, SniperRule
from src.models.profile import EnrichedDoctorProfile
from src.models.search import HealthcareProviderCard
from src.pipelines.storage import DoctolibStorage


class DryRunBookingAutomator:
    """
    Evaluates live availability against SniperRule and executes safe Dry-Run (Mock)
    booking reservation intents without spamming real medical clinics.
    """

    def __init__(
        self,
        rule: SniperRule,
        storage: DoctolibStorage,
        extractor: DoctolibExtractor | None = None,
    ) -> None:
        self.rule = rule
        self.storage = storage
        self.extractor = extractor

    def select_matching_slot(self, avail: ComputedAvailability) -> str | None:
        if avail.wait_time_days is None or avail.next_slot_iso is None:
            return None
        if avail.wait_time_days > self.rule.max_wait_days:
            return None
        if self.rule.allow_new_patients_only and not avail.allow_new_patients:
            return None
        if (
            self.rule.insurance_sector
            and avail.insurance_sector.lower() != self.rule.insurance_sector.lower()
        ):
            return None

        candidates = avail.sample_slots if avail.sample_slots else [avail.next_slot_iso]
        for slot_iso in candidates:
            try:
                dt = datetime.fromisoformat(slot_iso)
                if self.rule.preferred_hour_start <= dt.hour <= self.rule.preferred_hour_end:
                    return slot_iso
            except ValueError:
                continue
        return None

    def evaluate_and_reserve_mock(
        self,
        provider: HealthcareProviderCard,
        avail: ComputedAvailability,
        enrich_profile: bool = True,
    ) -> BookingIntentPayload | None:
        if self.rule.require_telehealth and not provider.is_telehealth:
            return None

        chosen_slot = self.select_matching_slot(avail)
        if not chosen_slot or avail.wait_time_days is None:
            return None

        enriched: EnrichedDoctorProfile | None = None
        if enrich_profile and self.extractor is not None:
            try:
                enriched = self.extractor.fetch_enriched_profile(
                    profile_slug=provider.profile_slug,
                    practice_id=provider.practice_id,
                )
                self.storage.apply_profile_enrichment(
                    enriched, fallback_practice_id=provider.practice_id
                )
            except Exception as exc:
                logger.warning(
                    f"Optional profile enrichment skipped for {provider.profile_slug}: {exc}"
                )

        address_parts = [
            p
            for p in (
                provider.location.address,
                f"{provider.location.zipcode or ''} {provider.location.city or ''}".strip(),
            )
            if p
        ]
        intent = BookingIntentPayload(
            profile_id=provider.profile_id,
            practice_id=provider.practice_id,
            visit_motive_id=avail.visit_motive_id,
            agenda_ids=provider.effective_agenda_ids,
            doctor_name=provider.full_display_name,
            specialty=provider.speciality.name or provider.speciality.slug or "Facharzt",
            city=provider.location.city or "",
            address=", ".join(address_parts),
            clinic_phone=enriched.primary_phone if enriched else None,
            opening_hours=enriched.primary_opening_hours if enriched else [],
            insurance_sector=avail.insurance_sector,
            target_slot_iso=chosen_slot,
            wait_time_days=avail.wait_time_days,
            booking_url=provider.profile_url,
        )

        self.storage.record_booking_intent(intent, status="MOCK_RESERVED")
        logger.info(
            f"[DRY-RUN BOOKING] Reserved mock intent for {intent.doctor_name} "
            f"({intent.insurance_sector.upper()}) @ {intent.target_slot_iso} "
            f"(wait: {intent.wait_time_days}d)"
        )
        return intent
