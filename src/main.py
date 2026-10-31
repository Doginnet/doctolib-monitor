"""Primary application entrypoint: starts the Doctolib Germany Telegram Bot & background slot poller."""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.panel import Panel

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config.settings import settings
from src.bot.telegram_bot import DoctolibTelegramBot
from src.pipelines.storage import DoctolibStorage
from src.utils.logger import setup_logger


console = Console()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="doctolib-bot",
        description="Doctolib Germany Telegram Appointment Monitor & Background Poller",
    )
    parser.add_argument(
        "--poll-minutes",
        type=float,
        default=settings.bot_poll_interval_minutes,
        help=f"Background Doctolib check interval in minutes (default from .env: {settings.bot_poll_interval_minutes:g})",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logger(level=args.log_level)

    if not settings.telegram_bot_token.strip():
        console.print(
            Panel.fit(
                "[bold red]TELEGRAM_BOT_TOKEN is not configured![/bold red]\n"
                "Open [cyan].env[/cyan] and paste your @BotFather token into [yellow]TELEGRAM_BOT_TOKEN=...[/yellow]\n\n"
                "Tip: To launch the standalone Scraper Engineer Web Console instead, run:\n"
                "  [green]uv run doctolib-web[/green]\n"
                "Or for a one-off CLI benchmark sweep:\n"
                "  [green]uv run doctolib-cli --city berlin --specialty kardiologie[/green]",
                border_style="red",
            )
        )
        return 1

    storage = DoctolibStorage(settings.resolved_db_path())
    bot_service = DoctolibTelegramBot(
        storage=storage,
        poll_interval_minutes=args.poll_minutes,
    )

    console.print(
        Panel.fit(
            "[bold cyan]Doctolib Germany — Telegram Appointment Monitor[/bold cyan]\n"
            f"Poll Interval: [green]every {args.poll_minutes:g} min[/green] | "
            f"Dedup Window: [yellow]{settings.bot_alert_dedup_hours:g}h[/yellow] | "
            f"SQLite DB: [white]{settings.resolved_db_path().name}[/white]\n"
            "[dim]Press Ctrl+C to stop the bot cleanly.[/dim]",
            border_style="cyan",
        )
    )

    app = bot_service.build_application()
    app.run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
