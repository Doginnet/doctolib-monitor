"""Standalone headless CLI scraper runner for terminal sweeps and GitHub Actions CI/CD."""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config.cities import DEFAULT_CI_SWEEP_TARGETS, GERMAN_CITIES, MEDICAL_SPECIALTIES
from config.settings import settings
from src.alerts.dispatcher import AlertDispatcher
from src.pipelines.runner import SweepRunRequest, execute_doctolib_sweep
from src.pipelines.storage import DoctolibStorage
from src.utils.logger import setup_logger


console = Console()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="doctolib-cli",
        description="Doctolib Germany (doctolib.de) Headless CLI Scraper & Benchmark Runner",
    )
    parser.add_argument(
        "--city",
        default="berlin",
        help=f"German city preset ({', '.join(GERMAN_CITIES.keys())})",
    )
    parser.add_argument(
        "--specialty",
        default="kardiologie",
        help=f"Specialty keyword/slug (e.g. {', '.join(list(MEDICAL_SPECIALTIES.keys())[:6])})",
    )
    parser.add_argument(
        "--ci-sweep",
        action="store_true",
        help="Run curated multi-city & multi-specialty benchmark sweep (Berlin, München, Hamburg)",
    )
    parser.add_argument(
        "--insurance",
        choices=["public", "private", "both"],
        default="both",
        help="German insurance sector: public (GKV), private (PKV), or both (default: both)",
    )
    parser.add_argument(
        "--max-doctors",
        type=int,
        default=20,
        help="Max providers per (city, specialty, insurance) query (ignored when --all is set)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scrape complete directory pages and run soft-deactivation lifecycle sync",
    )
    parser.add_argument(
        "--sniper-days",
        type=float,
        default=settings.sniper_max_wait_days,
        help="Urgent slot threshold in wait days (default: 7.0)",
    )
    parser.add_argument(
        "--auto-book-mock",
        action="store_true",
        help="Activate Dry-Run Booking Automator & deep clinic phone/hours enrichment on urgent matches",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Dispatch urgent slot alerts via n8n/Slack/Telegram (uses Safe Mock Mode if unconfigured)",
    )
    parser.add_argument(
        "--max-enrichments",
        type=int,
        default=8,
        help="Max deep profile (/profiles/{slug}.json) enrichments per sweep to stay polite",
    )
    parser.add_argument(
        "--hour-start",
        type=int,
        default=settings.sniper_preferred_hour_start,
        help="Preferred earliest appointment hour (0-23)",
    )
    parser.add_argument(
        "--hour-end",
        type=int,
        default=settings.sniper_preferred_hour_end,
        help="Preferred latest appointment hour (0-23)",
    )
    parser.add_argument(
        "--include-existing-patients",
        action="store_true",
        help="Allow slots reserved for existing patients (disables allow_new_patients_only filter)",
    )
    parser.add_argument(
        "--telehealth-only",
        action="store_true",
        help="Match only practitioners offering video consultations (telehealth)",
    )
    parser.add_argument(
        "--min-delay",
        type=float,
        default=settings.doctolib_min_delay,
        help="Minimum polite delay between requests in seconds",
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=settings.doctolib_max_delay,
        help="Maximum polite delay between requests in seconds",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def render_summary_tables(storage: DoctolibStorage, sniper_days: float) -> None:
    urgent = storage.get_latest_availability_records(
        max_wait_days=sniper_days,
        active_only=True,
        limit=15,
    )
    sniper_table = Table(
        title=f"Top Urgent Doctolib Slots (Wait <= {sniper_days:.1f} Days)",
        header_style="bold cyan",
    )
    sniper_table.add_column("Doctor / Clinic", style="bold white", max_width=30)
    sniper_table.add_column("City", style="cyan")
    sniper_table.add_column("Specialty", style="magenta", max_width=22)
    sniper_table.add_column("Sector", justify="center")
    sniper_table.add_column("Wait (d)", justify="right", style="bold green")
    sniper_table.add_column("Earliest Slot", style="yellow")
    sniper_table.add_column("Phone", style="dim")

    for row in urgent:
        sec_badge = (
            "[bold blue]GKV[/bold blue]"
            if row["insurance_sector"] == "public"
            else "[bold gold1]PKV[/bold gold1]"
        )
        slot_short = (row["next_slot_iso"] or "")[:16].replace("T", " ")
        sniper_table.add_row(
            row["full_name"],
            row["city_name"],
            row["specialty_name"],
            sec_badge,
            f"{row['wait_time_days']:.1f}",
            slot_short,
            row.get("landline_number") or "—",
        )

    if urgent:
        console.print(sniper_table)
    else:
        console.print(f"[yellow]No slots found within {sniper_days} days in current database.[/yellow]")

    gap_rows = storage.get_insurance_gap_summary(active_only=True)
    if gap_rows:
        gap_table = Table(
            title="GKV (Public) vs. PKV (Private) Wait-Time Gap Analytics",
            header_style="bold magenta",
        )
        gap_table.add_column("City", style="cyan")
        gap_table.add_column("Specialty", style="white")
        gap_table.add_column("Insurance", justify="center")
        gap_table.add_column("Checked", justify="right")
        gap_table.add_column("Bookable", justify="right")
        gap_table.add_column("Min Wait (d)", justify="right", style="green")
        gap_table.add_column("Avg Wait (d)", justify="right", style="bold yellow")

        for g in gap_rows:
            ins_label = "GKV (Public)" if g["insurance_sector"] == "public" else "PKV (Private)"
            min_w = f"{g['min_wait_days']:.1f}" if g["min_wait_days"] is not None else "N/A"
            avg_w = f"{g['avg_wait_days']:.1f}" if g["avg_wait_days"] is not None else "N/A"
            gap_table.add_row(
                g["city_name"] or "",
                g["specialty_name"] or "",
                ins_label,
                str(g["total_checked"]),
                str(g["with_open_slots"]),
                min_w,
                avg_w,
            )
        console.print(gap_table)


def run_cli(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logger(level=args.log_level)

    dispatcher = AlertDispatcher()
    targets_count = len(DEFAULT_CI_SWEEP_TARGETS) if args.ci_sweep else 1
    sectors = ["public", "private"] if args.insurance == "both" else [args.insurance]

    console.print(
        Panel.fit(
            f"[bold cyan]Doctolib Germany Real-Time Slot Sniper & Monitor (CLI)[/bold cyan]\n"
            f"Targets: [white]{targets_count}[/white] | Sectors: [yellow]{', '.join(sectors).upper()}[/yellow] | "
            f"Sniper Threshold: [green]<={args.sniper_days}d[/green] | "
            f"Alert Mode: [magenta]{'SIMULATION (MOCK)' if dispatcher.is_mock_mode else 'LIVE WEBHOOKS'}[/magenta]",
            border_style="cyan",
        )
    )

    run_config = SweepRunRequest(
        city=args.city,
        specialty=args.specialty,
        insurance=args.insurance,
        max_doctors=args.max_doctors,
        sniper_days=args.sniper_days,
        max_enrichments=args.max_enrichments,
        preferred_hour_start=args.hour_start,
        preferred_hour_end=args.hour_end,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        log_level=args.log_level,
        ci_sweep=args.ci_sweep,
        scrape_all=args.all,
        auto_book_mock=args.auto_book_mock,
        notify=args.notify,
        allow_new_patients_only=not args.include_existing_patients,
        require_telehealth=args.telehealth_only,
    )

    summary = execute_doctolib_sweep(run_config)
    if summary.status == "failed":
        console.print(f"[bold red]Sweep failed:[/bold red] {summary.error_message}")
        return 1

    console.print(
        f"\n[bold green]Sweep Completed ({summary.elapsed_seconds}s):[/bold green] "
        f"{summary.providers_seen} provider records | "
        f"{summary.calendars_checked} live calendars checked | "
        f"{summary.sniper_matches} sniper intents generated.\n"
    )
    storage = DoctolibStorage(settings.resolved_db_path())
    render_summary_tables(storage, sniper_days=args.sniper_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
