"""Background subscription poller with 24h slot deduplication and non-blocking Doctolib checks."""

from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

from loguru import logger

from config.settings import settings
from src.alerts.dispatcher import AlertDispatcher
from src.core.client import ResilientHttpClient
from src.extractors.doctolib import DoctolibExtractor
from src.models.availability import ComputedAvailability
from src.models.booking import BookingIntentPayload
from src.pipelines.storage import DoctolibStorage


@dataclass
class MatchedSlotAlert:
    subscription_id: int
    chat_id: int
    profile_id: int
    practice_id: int
    doctor_name: str
    specialty_name: str
    procedure_name: str
    insurance_sector: str
    slot_iso: str
    wait_time_days: float
    city: str
    address: str
    clinic_phone: str | None
    booking_url: str


AlertSenderFn = Callable[[MatchedSlotAlert, str], Awaitable[None]]
ExtractorContextFactory = Callable[[], AbstractContextManager[DoctolibExtractor]]


@contextmanager
def default_extractor_context() -> Iterator[DoctolibExtractor]:
    with ResilientHttpClient() as client:
        client.warmup()
        yield DoctolibExtractor(client)


def pick_slot_in_time_window(
    avail: ComputedAvailability,
    max_wait_days: float,
    hour_start: int,
    hour_end: int,
) -> str | None:
    """Selects the earliest ISO slot matching both max wait horizon and user's preferred hours."""
    if avail.wait_time_days is None or avail.next_slot_iso is None:
        return None
    if avail.wait_time_days > max_wait_days:
        return None

    candidates = avail.sample_slots if avail.sample_slots else [avail.next_slot_iso]
    for slot_iso in candidates:
        try:
            dt = datetime.fromisoformat(slot_iso)
            if hour_start <= dt.hour <= hour_end:
                return slot_iso
        except ValueError:
            continue
    return None


def format_slot_alert_html(alert: MatchedSlotAlert) -> str:
    """Formats a clean, high-signal Telegram HTML notification card."""
    sector_label = (
        "GKV (Public / Gesetzlich)"
        if alert.insurance_sector == "public"
        else "PKV (Private / Privat)"
    )
    slot_pretty = alert.slot_iso[:16].replace("T", " ")
    loc_line = alert.address or alert.city or "Deutschland"
    phone_line = (
        f"\n<b>Phone:</b> <code>{html.escape(alert.clinic_phone)}</code>"
        if alert.clinic_phone
        else ""
    )
    proc_line = (
        f"\n<b>Procedure:</b> {html.escape(alert.procedure_name)}"
        if alert.procedure_name
        else ""
    )

    return (
        f"<b>New Doctolib Appointment Found ({alert.wait_time_days:.1f}d wait)</b>\n\n"
        f"<b>Doctor / Clinic:</b> {html.escape(alert.doctor_name)}\n"
        f"<b>Specialty:</b> {html.escape(alert.specialty_name)}"
        f"{proc_line}\n"
        f"<b>Insurance:</b> {html.escape(sector_label)}\n"
        f"<b>Earliest Slot:</b> <code>{html.escape(slot_pretty)}</code>\n"
        f"<b>Location:</b> {html.escape(loc_line)}"
        f"{phone_line}"
    )


class SubscriptionPoller:
    """
    Periodically evaluates active user subscriptions against Doctolib's live calendar API,
    enforces per-slot deduplication, and dispatches instant Telegram (and optional n8n/Slack) alerts.
    """

    def __init__(
        self,
        storage: DoctolibStorage,
        alert_sender: AlertSenderFn,
        *,
        extractor_factory: ExtractorContextFactory = default_extractor_context,
        poll_interval_minutes: float | None = None,
        max_doctors_per_search: int | None = None,
        dedup_hours: float | None = None,
        max_alerts_per_cycle: int | None = None,
        webhook_dispatcher: AlertDispatcher | None = None,
    ) -> None:
        self.storage = storage
        self.alert_sender = alert_sender
        self.extractor_factory = extractor_factory
        self.poll_interval_minutes = (
            settings.bot_poll_interval_minutes
            if poll_interval_minutes is None
            else poll_interval_minutes
        )
        self.max_doctors_per_search = (
            settings.bot_max_doctors_per_search
            if max_doctors_per_search is None
            else max_doctors_per_search
        )
        self.dedup_hours = (
            settings.bot_alert_dedup_hours if dedup_hours is None else dedup_hours
        )
        self.max_alerts_per_cycle = (
            settings.bot_max_alerts_per_cycle
            if max_alerts_per_cycle is None
            else max_alerts_per_cycle
        )
        self.webhook_dispatcher = webhook_dispatcher or AlertDispatcher(
            telegram_bot_token="",
            telegram_chat_id="",
        )
        self._check_lock = asyncio.Lock()

    def _evaluate_subscription_sync(
        self,
        sub: dict[str, Any],
        extractor: DoctolibExtractor,
    ) -> list[MatchedSlotAlert]:
        """Runs blocking HTTP extraction for a single subscription inside a worker thread."""
        sub_id = int(sub["id"])
        chat_id = int(sub["chat_id"])
        sector = str(sub["insurance_sector"]).lower()
        max_wait = float(sub["max_wait_days"])
        h_start = int(sub["hour_start"])
        h_end = int(sub["hour_end"])
        matches: list[MatchedSlotAlert] = []

        if sub["sub_type"] == "doctor":
            agenda_ids = [int(a) for a in (sub.get("agenda_ids") or []) if a]
            motive_id = int(sub.get("visit_motive_id") or 0)
            practice_id = int(sub.get("practice_id") or 0)
            profile_id = int(sub.get("profile_id") or 0)
            doctor_slug = str(sub.get("doctor_slug") or "")
            if not (agenda_ids and motive_id and practice_id and doctor_slug):
                return []

            avail = extractor.fetch_direct_motive_availability(
                profile_id=profile_id,
                practice_id=practice_id,
                profile_slug=doctor_slug,
                visit_motive_id=motive_id,
                motive_name=str(sub.get("visit_motive_name") or ""),
                agenda_ids=agenda_ids,
                insurance_sector=sector,
                telehealth=bool(sub.get("telehealth")),
                limit_days=15,
            )
            self.storage.insert_slot_snapshot(avail)
            chosen_slot = pick_slot_in_time_window(avail, max_wait, h_start, h_end)
            if chosen_slot and avail.wait_time_days is not None:
                already_sent = self.storage.was_slot_alert_sent(
                    subscription_id=sub_id,
                    profile_id=profile_id,
                    practice_id=practice_id,
                    slot_iso=chosen_slot,
                    dedup_hours=self.dedup_hours,
                )
                if not already_sent:
                    matches.append(
                        MatchedSlotAlert(
                            subscription_id=sub_id,
                            chat_id=chat_id,
                            profile_id=profile_id,
                            practice_id=practice_id,
                            doctor_name=str(sub.get("doctor_name") or doctor_slug),
                            specialty_name=str(sub.get("specialty_name") or "Facharzt"),
                            procedure_name=str(sub.get("visit_motive_name") or ""),
                            insurance_sector=sector,
                            slot_iso=chosen_slot,
                            wait_time_days=avail.wait_time_days,
                            city=str(sub.get("city_name") or ""),
                            address=str(sub.get("clinic_address") or ""),
                            clinic_phone=sub.get("clinic_phone"),
                            booking_url=str(
                                sub.get("booking_url")
                                or f"https://www.doctolib.de/profiles/{doctor_slug}"
                            ),
                        )
                    )
        else:
            # Mode 1: City + Specialty directory scan
            city_slug = str(sub.get("city_slug") or "berlin")
            spec_slug = str(sub.get("specialty_slug") or "allgemeinmedizin")

            for card in extractor.iter_providers(
                keyword=spec_slug,
                city_key=city_slug,
                insurance_sector=sector,
                max_doctors=self.max_doctors_per_search,
            ):
                self.storage.upsert_provider(
                    card=card,
                    city_key=city_slug,
                    search_keyword=spec_slug,
                    insurance_sector=sector,
                )
                if not card.is_bookable:
                    continue

                try:
                    avail = extractor.fetch_provider_availability(
                        provider=card,
                        insurance_sector=sector,
                        limit_days=15,
                    )
                except Exception as exc:
                    logger.warning(f"Poller skipped {card.profile_slug}: {exc}")
                    continue

                if avail is None:
                    continue

                self.storage.insert_slot_snapshot(avail)
                chosen_slot = pick_slot_in_time_window(avail, max_wait, h_start, h_end)
                if not chosen_slot or avail.wait_time_days is None:
                    extractor.client.polite_sleep()
                    continue

                if self.storage.was_slot_alert_sent(
                    subscription_id=sub_id,
                    profile_id=card.profile_id,
                    practice_id=card.practice_id,
                    slot_iso=chosen_slot,
                    dedup_hours=self.dedup_hours,
                ):
                    extractor.client.polite_sleep()
                    continue

                phone: str | None = None
                try:
                    enriched = extractor.fetch_enriched_profile(
                        profile_slug=card.profile_slug,
                        practice_id=card.practice_id,
                    )
                    self.storage.apply_profile_enrichment(
                        enriched, fallback_practice_id=card.practice_id
                    )
                    phone = enriched.primary_phone
                except Exception:
                    phone = None

                addr_parts = [
                    card.location.address or "",
                    f"{card.location.zipcode or ''} {card.location.city or ''}".strip(),
                ]
                matches.append(
                    MatchedSlotAlert(
                        subscription_id=sub_id,
                        chat_id=chat_id,
                        profile_id=card.profile_id,
                        practice_id=card.practice_id,
                        doctor_name=card.full_display_name,
                        specialty_name=card.speciality.name
                        or str(sub.get("specialty_name") or spec_slug),
                        procedure_name=avail.motive_name,
                        insurance_sector=sector,
                        slot_iso=chosen_slot,
                        wait_time_days=avail.wait_time_days,
                        city=card.location.city or str(sub.get("city_name") or city_slug),
                        address=", ".join(p for p in addr_parts if p),
                        clinic_phone=phone,
                        booking_url=str(card.profile_url),
                    )
                )

                if len(matches) >= self.max_alerts_per_cycle:
                    break

                extractor.client.polite_sleep()

        self.storage.touch_subscription_checked(sub_id)
        return matches

    async def _deliver_matches(self, matches: list[MatchedSlotAlert]) -> int:
        sent_count = 0
        for alert in matches[: self.max_alerts_per_cycle]:
            msg_html = format_slot_alert_html(alert)
            await self.alert_sender(alert, msg_html)
            self.storage.record_sent_slot_alert(
                subscription_id=alert.subscription_id,
                chat_id=alert.chat_id,
                profile_id=alert.profile_id,
                practice_id=alert.practice_id,
                slot_iso=alert.slot_iso,
            )
            # Forward to optional external n8n / Slack webhook if configured
            if not self.webhook_dispatcher.is_mock_mode:
                intent = BookingIntentPayload(
                    mode="TELEGRAM_SUBSCRIPTION_ALERT",
                    profile_id=alert.profile_id,
                    practice_id=alert.practice_id,
                    visit_motive_id=0,
                    doctor_name=alert.doctor_name,
                    specialty=alert.specialty_name,
                    city=alert.city,
                    address=alert.address,
                    clinic_phone=alert.clinic_phone,
                    insurance_sector=alert.insurance_sector,
                    target_slot_iso=alert.slot_iso,
                    wait_time_days=alert.wait_time_days,
                    booking_url=alert.booking_url,
                )
                await asyncio.to_thread(self.webhook_dispatcher.dispatch, intent)
            sent_count += 1
        return sent_count

    async def check_single_subscription(self, sub_id: int) -> int:
        """Evaluates one subscription immediately (triggered on creation or 'Check Now' click)."""
        sub = self.storage.get_subscription(sub_id)
        if not sub or not sub.get("is_active"):
            return 0

        def _run_one() -> list[MatchedSlotAlert]:
            with self.extractor_factory() as extractor:
                return self._evaluate_subscription_sync(sub, extractor)

        async with self._check_lock:
            matches = await asyncio.to_thread(_run_one)
            return await self._deliver_matches(matches)

    async def poll_all_once(self) -> int:
        """Runs one complete polling sweep across all active subscriptions."""
        active_subs = self.storage.list_subscriptions(active_only=True)
        if not active_subs:
            logger.debug("Poller cycle skipped: 0 active subscriptions.")
            return 0

        logger.info(f"Poller cycle started: checking {len(active_subs)} active subscription(s)...")

        def _run_batch() -> list[MatchedSlotAlert]:
            collected: list[MatchedSlotAlert] = []
            with self.extractor_factory() as extractor:
                for sub in active_subs:
                    try:
                        collected.extend(self._evaluate_subscription_sync(sub, extractor))
                    except Exception as exc:
                        logger.error(f"Error polling subscription #{sub.get('id')}: {exc}")
            return collected

        async with self._check_lock:
            all_matches = await asyncio.to_thread(_run_batch)
            total_sent = 0
            # Group by subscription to respect max_alerts_per_cycle per subscription
            by_sub: dict[int, list[MatchedSlotAlert]] = {}
            for m in all_matches:
                by_sub.setdefault(m.subscription_id, []).append(m)
            for sub_matches in by_sub.values():
                total_sent += await self._deliver_matches(sub_matches)

        logger.info(f"Poller cycle finished: {total_sent} new slot alert(s) delivered.")
        return total_sent

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Background asyncio task polling every `poll_interval_minutes` until `stop_event` is set."""
        interval_sec = max(30.0, float(self.poll_interval_minutes) * 60.0)
        logger.info(
            f"Background SubscriptionPoller active (interval={self.poll_interval_minutes} min)"
        )
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
                break
            except asyncio.TimeoutError:
                try:
                    await self.poll_all_once()
                except Exception as exc:
                    logger.error(f"Unhandled error in background poller loop: {exc}")
