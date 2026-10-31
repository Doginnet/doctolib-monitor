"""Offline mock tests for Telegram Bot handlers, Doctolib URL funnel parsing, and 10-min SubscriptionPoller."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.poller import MatchedSlotAlert, SubscriptionPoller
from src.bot.telegram_bot import DoctolibTelegramBot
from src.models.availability import ComputedAvailability
from src.models.funnel import BookingFunnelInfo, extract_doctolib_slug_and_pid
from src.models.profile import EnrichedDoctorProfile
from src.models.search import HcpSearchResponse
from src.pipelines.storage import DoctolibStorage


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _make_fake_message_update(text: str, chat_id: int = 777001) -> tuple[Any, Any]:
    """Builds lightweight mocked Telegram Update & Context objects for command/text handlers."""
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()

    message = MagicMock()
    message.text = text
    message.reply_text = AsyncMock(return_value=status_msg)

    chat = MagicMock()
    chat.id = chat_id

    update = MagicMock()
    update.effective_chat = chat
    update.effective_message = message

    context = MagicMock()
    context.user_data = {}
    return update, context


def _make_fake_callback_update(
    callback_data: str,
    user_data: dict[str, Any],
    chat_id: int = 777001,
) -> tuple[Any, Any]:
    """Builds lightweight mocked Telegram CallbackQuery Update & shared user_data Context."""
    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    chat = MagicMock()
    chat.id = chat_id

    update = MagicMock()
    update.callback_query = query
    update.effective_chat = chat

    context = MagicMock()
    context.user_data = user_data
    return update, context


@pytest.mark.unit
def test_doctolib_url_slug_and_funnel_procedure_parsing() -> None:
    slug, pid = extract_doctolib_slug_and_pid(
        "https://www.doctolib.de/kardiologie/berlin/anne-winkelmann?pid=practice-544676"
    )
    assert slug == "anne-winkelmann"
    assert pid == 544676

    slug2, pid2 = extract_doctolib_slug_and_pid("https://www.doctolib.de/profiles/ramona-rus")
    assert slug2 == "ramona-rus"
    assert pid2 is None

    with pytest.raises(ValueError):
        extract_doctolib_slug_and_pid("https://example.com/not-doctolib/doctor")

    raw_funnel = json.loads(
        (FIXTURES_DIR / "booking_funnel_sample.json").read_text(encoding="utf-8")
    )
    funnel = BookingFunnelInfo.from_api_response(
        raw_funnel,
        fallback_slug="anne-winkelmann",
        preferred_practice_id=544676,
    )
    assert funnel.profile_id == 765796
    assert funnel.doctor_name == "Dr. med. Anne Winkelmann"
    assert funnel.city == "Berlin"
    assert funnel.landline_number == "030 9876543"
    assert len(funnel.procedures) == 2

    # Disabled agenda 1999999 must be filtered out, leaving only active agenda 1697876
    echokardio = funnel.procedures[0]
    assert echokardio.visit_motive_id == 10841912
    assert echokardio.name == "Echokardiografie Neupatient"
    assert echokardio.agenda_ids == [1697876]
    assert echokardio.practice_id == 544676


@pytest.mark.unit
def test_bot_doctor_url_flow_and_callback_wizard_with_mocks(tmp_path: Path) -> None:
    async def _scenario() -> None:
        storage = DoctolibStorage(tmp_path / "bot_url_flow.db")
        raw_funnel = json.loads(
            (FIXTURES_DIR / "booking_funnel_sample.json").read_text(encoding="utf-8")
        )
        sample_funnel = BookingFunnelInfo.from_api_response(raw_funnel)

        mock_funnel_loader = MagicMock(return_value=sample_funnel)
        mock_extractor = MagicMock()
        mock_extractor.fetch_direct_motive_availability.return_value = ComputedAvailability(
            profile_id=765796,
            practice_id=544676,
            visit_motive_id=10841912,
            motive_name="Echokardiografie Neupatient",
            insurance_sector="public",
            next_slot_iso="2026-10-31T17:30:00.000+01:00",
            wait_time_days=1.2,
            open_slots_count=2,
            sample_slots=["2026-10-31T17:30:00.000+01:00"],
        )

        @contextmanager
        def fake_extractor_ctx() -> Iterator[Any]:
            yield mock_extractor

        bot = DoctolibTelegramBot(
            storage=storage,
            extractor_factory=fake_extractor_ctx,
            funnel_loader=mock_funnel_loader,
            poll_interval_minutes=10.0,
        )
        # Intercept outgoing Telegram alerts via AsyncMock instead of hitting api.telegram.org
        sent_alerts: list[tuple[MatchedSlotAlert, str]] = []

        async def capture_alert(alert: MatchedSlotAlert, html_msg: str) -> None:
            sent_alerts.append((alert, html_msg))

        bot.poller.alert_sender = capture_alert

        # 1. /start command
        upd_start, ctx = _make_fake_message_update("/start", chat_id=99001)
        await bot.handle_start(upd_start, ctx)
        upd_start.effective_message.reply_text.assert_awaited_once()

        # 2. User pastes a Doctolib doctor URL
        doctor_url = "https://www.doctolib.de/kardiologie/berlin/anne-winkelmann?pid=practice-544676"
        upd_url, ctx = _make_fake_message_update(doctor_url, chat_id=99001)
        await bot.handle_text_message(upd_url, ctx)
        mock_funnel_loader.assert_called_once_with(doctor_url)
        assert ctx.user_data["draft"]["sub_type"] == "doctor"

        # 3. User clicks procedure #0 -> GKV (public) -> <= 7 days -> Evening (16:00-20:00)
        for cb_data in ("m2_proc:0", "wiz_ins:public", "wiz_wait:7", "wiz_time:evening"):
            cb_upd, ctx = _make_fake_callback_update(cb_data, ctx.user_data, chat_id=99001)
            await bot.handle_callback(cb_upd, ctx)

        # 4. Verify subscription saved in SQLite and instant slot alert delivered
        subs = storage.list_subscriptions(chat_id=99001, active_only=True)
        assert len(subs) == 1
        saved = subs[0]
        assert saved["sub_type"] == "doctor"
        assert saved["doctor_slug"] == "anne-winkelmann"
        assert saved["visit_motive_id"] == 10841912
        assert saved["agenda_ids"] == [1697876]
        assert saved["insurance_sector"] == "public"
        assert saved["hour_start"] == 16
        assert saved["hour_end"] == 20

        mock_extractor.fetch_direct_motive_availability.assert_called_once()
        assert len(sent_alerts) == 1
        assert "Dr. med. Anne Winkelmann" in sent_alerts[0][1]
        assert "Echokardiografie Neupatient" in sent_alerts[0][1]

        # 5. User clicks "Booked / Stop This Alert" button -> soft-deactivates subscription
        stop_upd, ctx = _make_fake_callback_update(
            f"sub_stop:{saved['id']}", {}, chat_id=99001
        )
        await bot.handle_callback(stop_upd, ctx)
        assert len(storage.list_subscriptions(chat_id=99001, active_only=True)) == 0
        # Row is preserved with is_active=False (non-destructive lifecycle)
        all_subs = storage.list_subscriptions(chat_id=99001, active_only=False)
        assert len(all_subs) == 1
        assert all_subs[0]["is_active"] is False

    asyncio.run(_scenario())


@pytest.mark.unit
def test_bot_city_specialty_flow_and_poller_deduplication_with_mocks(tmp_path: Path) -> None:
    async def _scenario() -> None:
        storage = DoctolibStorage(tmp_path / "bot_city_flow.db")
        raw_search = json.loads(
            (FIXTURES_DIR / "hcp_search_sample.json").read_text(encoding="utf-8")
        )
        doc_card = HcpSearchResponse.model_validate(raw_search).healthcare_providers[0]
        raw_profile = json.loads(
            (FIXTURES_DIR / "doctor_profile_sample.json").read_text(encoding="utf-8")
        )
        enriched_prof = EnrichedDoctorProfile.from_api_response(raw_profile)

        mock_extractor = MagicMock()
        mock_extractor.client.polite_sleep = MagicMock()
        mock_extractor.iter_providers.return_value = iter([doc_card])
        mock_extractor.fetch_provider_availability.return_value = ComputedAvailability(
            profile_id=doc_card.profile_id,
            practice_id=doc_card.practice_id,
            visit_motive_id=13898103,
            motive_name="Erstuntersuchung Neupatient:in",
            insurance_sector="private",
            next_slot_iso="2026-10-31T10:30:00.000+01:00",
            wait_time_days=1.0,
            open_slots_count=3,
            sample_slots=["2026-10-31T10:30:00.000+01:00"],
        )
        mock_extractor.fetch_enriched_profile.return_value = enriched_prof

        @contextmanager
        def fake_extractor_ctx() -> Iterator[Any]:
            yield mock_extractor

        bot = DoctolibTelegramBot(
            storage=storage,
            extractor_factory=fake_extractor_ctx,
            poll_interval_minutes=10.0,
        )
        alert_mock = AsyncMock()
        bot.poller = SubscriptionPoller(
            storage=storage,
            alert_sender=alert_mock,
            extractor_factory=fake_extractor_ctx,
            poll_interval_minutes=10.0,
            dedup_hours=24.0,
        )

        # Walk through Mode 1 inline button wizard: Berlin -> Kardiologie -> PKV -> <=14d -> Morning (07-12)
        user_state: dict[str, Any] = {}
        for step_cb in (
            "menu:mode_city",
            "wiz_city:berlin",
            "wiz_spec:kardiologie",
            "wiz_ins:private",
            "wiz_wait:14",
            "wiz_time:morning",
        ):
            upd, ctx = _make_fake_callback_update(step_cb, user_state, chat_id=55002)
            await bot.handle_callback(upd, ctx)

        assert alert_mock.await_count == 1
        first_alert: MatchedSlotAlert = alert_mock.await_args_list[0].args[0]
        assert first_alert.doctor_name == "Dr. med. Ramona Rus"
        assert first_alert.clinic_phone == "030 76725289"

        # Reset iterator for second 10-minute background poll cycle returning the EXACT same slot
        mock_extractor.iter_providers.return_value = iter([doc_card])
        second_cycle_sent = await bot.poller.poll_all_once()

        # Deduplication guarantee: exact same slot within 24h must NOT spam the user again
        assert second_cycle_sent == 0
        assert alert_mock.await_count == 1

    asyncio.run(_scenario())
