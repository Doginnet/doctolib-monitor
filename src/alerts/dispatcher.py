"""Pluggable webhook & messaging dispatcher for n8n, Slack, and Telegram with Mock-Safe fallback."""

from __future__ import annotations

import requests as std_requests
from loguru import logger

from config.settings import settings
from src.models.booking import BookingIntentPayload


class AlertDispatcher:
    """Routes urgent slot intents to n8n webhooks, Slack, or Telegram; falls back to Simulation Mode safely."""

    def __init__(
        self,
        webhook_url: str | None = None,
        slack_webhook_url: str | None = None,
        telegram_bot_token: str | None = None,
        telegram_chat_id: str | None = None,
    ) -> None:
        self.webhook_url = (
            settings.webhook_url if webhook_url is None else webhook_url
        ).strip()
        self.slack_webhook_url = (
            settings.slack_webhook_url if slack_webhook_url is None else slack_webhook_url
        ).strip()
        self.telegram_bot_token = (
            settings.telegram_bot_token if telegram_bot_token is None else telegram_bot_token
        ).strip()
        self.telegram_chat_id = (
            settings.telegram_chat_id if telegram_chat_id is None else telegram_chat_id
        ).strip()

    @property
    def is_mock_mode(self) -> bool:
        has_tg = bool(self.telegram_bot_token and self.telegram_chat_id)
        return not (bool(self.webhook_url) or bool(self.slack_webhook_url) or has_tg)

    @staticmethod
    def format_telegram_html(intent: BookingIntentPayload) -> str:
        sector_badge = "🏛️ GKV (Gesetzlich)" if intent.insurance_sector == "public" else "💳 PKV (Privat)"
        phone_line = f"\n📞 <b>Tel:</b> <code>{intent.clinic_phone}</code>" if intent.clinic_phone else ""
        return (
            f"🎯 <b>Doctolib Urgent Slot Match ({intent.wait_time_days}d wait)</b>\n"
            f"👨‍⚕️ <b>{intent.doctor_name}</b> — {intent.specialty}\n"
            f"🏥 {sector_badge}\n"
            f"📅 <b>Earliest Slot:</b> <code>{intent.target_slot_iso}</code>\n"
            f"📍 {intent.address or intent.city}{phone_line}\n"
            f"🔗 <a href=\"{intent.booking_url}\">Open Doctolib Booking Funnel</a>"
        )

    def dispatch(self, intent: BookingIntentPayload) -> dict[str, str]:
        results: dict[str, str] = {}

        if self.is_mock_mode:
            logger.info(
                f"[SIMULATION ALERT] {intent.doctor_name} | {intent.specialty} | "
                f"{intent.insurance_sector.upper()} | Slot: {intent.target_slot_iso} "
                f"({intent.wait_time_days}d) -> {intent.booking_url}"
            )
            return {"mock": "SIMULATED_OK"}

        if self.webhook_url:
            try:
                resp = std_requests.post(
                    self.webhook_url,
                    data=intent.model_dump_json(),
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
                results["webhook"] = f"HTTP_{resp.status_code}"
            except Exception as exc:
                logger.warning(f"n8n/Webhook delivery failed: {exc}")
                results["webhook"] = "ERROR"

        if self.slack_webhook_url:
            try:
                slack_text = (
                    f":dart: *Doctolib Slot Sniper ({intent.wait_time_days}d wait)*\n"
                    f"*{intent.doctor_name}* ({intent.specialty} — `{intent.insurance_sector.upper()}`)\n"
                    f"Slot: `{intent.target_slot_iso}` | {intent.address}\n"
                    f"<{intent.booking_url}|Book on Doctolib>"
                )
                resp = std_requests.post(
                    self.slack_webhook_url,
                    json={"text": slack_text},
                    timeout=10,
                )
                results["slack"] = f"HTTP_{resp.status_code}"
            except Exception as exc:
                logger.warning(f"Slack webhook delivery failed: {exc}")
                results["slack"] = "ERROR"

        if self.telegram_bot_token and self.telegram_chat_id:
            try:
                tg_url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
                resp = std_requests.post(
                    tg_url,
                    json={
                        "chat_id": self.telegram_chat_id,
                        "text": self.format_telegram_html(intent),
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=10,
                )
                results["telegram"] = f"HTTP_{resp.status_code}"
            except Exception as exc:
                logger.warning(f"Telegram delivery failed: {exc}")
                results["telegram"] = "ERROR"

        return results
