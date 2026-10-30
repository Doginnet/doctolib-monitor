"""Multi-channel notification & webhook dispatcher (n8n, Slack, Telegram) with Safe Mock fallback."""

from src.alerts.dispatcher import AlertDispatcher

__all__ = ["AlertDispatcher"]
