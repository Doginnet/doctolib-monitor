"""Rich CLI runner for Doctolib Germany Slot Sniper, Wait-Time Monitor & Dry-Run Booking Automator."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from loguru import logger
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
from src.booking.automator import DryRunBookingAutomator
from src.core.client import ResilientHttpClient
from src.extractors.doctolib import DoctolibExtractor
from src.models.booking import SniperRule
from src.pipelines.export import export_global_summary, export_scope_samples
from src.pipelines.storage import DoctolibStorage
from src.utils.logger import setup_logger


console = Console()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="doctolib-monitor",
        description="Doctolib Germany (doctolib.de) Real-Time Slot Sniper & Wait-Time Monitor",
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
        title=f"🎯 Top Urgent Doctolib Slots (Wait <= {sniper_days:.1f} Days)",
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
            title="⚖️ GKV (Public) vs. PKV (Private) Wait-Time Gap Analytics",
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logger(level=args.log_level)

    storage = DoctolibStorage(settings.resolved_db_path())
    samples_dir = settings.resolved_samples_dir()
    dispatcher = AlertDispatcher()

    targets: list[tuple[str, str]] = (
        DEFAULT_CI_SWEEP_TARGETS
        if args.ci_sweep
        else [(args.city.strip().lower(), args.specialty.strip().lower())]
    )
    sectors = ["public", "private"] if args.insurance == "both" else [args.insurance]
    max_docs = None if args.all else args.max_doctors

    console.print(
        Panel.fit(
            f"[bold cyan]Doctolib Germany Real-Time Slot Sniper & Monitor[/bold cyan]\n"
            f"Targets: [white]{len(targets)}[/white] | Sectors: [yellow]{', '.join(sectors).upper()}[/yellow] | "
            f"Sniper Threshold: [green]<={args.sniper_days}d[/green] | "
            f"Alert Mode: [magenta]{'SIMULATION (MOCK)' if dispatcher.is_mock_mode else 'LIVE WEBHOOKS'}[/magenta]",
            border_style="cyan",
        )
    )

    total_providers_seen = 0
    total_snapshots = 0
    total_sniper_matches = 0

    with ResilientHttpClient() as client:
        client.warmup()
        extractor = DoctolibExtractor(client)

        for city_key, specialty_kw in targets:
            enrichments_done = 0

            for sector in sectors:
                sweep_start = datetime.now(timezone.utc)
                rule = SniperRule(
                    max_wait_days=args.sniper_days,
                    insurance_sector=sector,
                    allow_new_patients_only=settings.sniper_allow_new_patients_only,
                    preferred_hour_start=settings.sniper_preferred_hour_start,
                    preferred_hour_end=settings.sniper_preferred_hour_end,
                )
                automator = DryRunBookingAutomator(
                    rule=rule,
                    storage=storage,
                    extractor=extractor,
                )

                for card in extractor.iter_providers(
                    keyword=specialty_kw,
                    city_key=city_key,
                    insurance_sector=sector,
                    max_doctors=max_docs,
                ):
                    storage.upsert_provider(
                        card=card,
                        city_key=city_key,
                        search_keyword=specialty_kw,
                        insurance_sector=sector,
                    )
                    total_providers_seen += 1

                    if not card.is_bookable:
                        continue

                    try:
                        avail = extractor.fetch_provider_availability(
                            provider=card,
                            insurance_sector=sector,
                            limit_days=15,
                        )
                    except Exception as avail_err:
                        logger.warning(
                            f"Availability fetch failed for {card.profile_slug}: {avail_err}"
                        )
                        continue

                    if avail is None:
                        continue

                    storage.insert_slot_snapshot(avail)
                    total_snapshots += 1

                    if args.auto_book_mock or args.notify:
                        should_enrich = enrichments_done < args.max_enrichments
                        intent = automator.evaluate_and_reserve_mock(
                            provider=card,
                            avail=avail,
                            enrich_profile=should_enrich,
                        )
                        if intent is not None:
                            total_sniper_matches += 1
                            if should_enrich:
                                enrichments_done += 1
                            if args.notify:
                                dispatcher.dispatch(intent)

                    client.polite_sleep()

                if args.all:
                    storage.deactivate_unseen_providers(
                        city_key=city_key,
                        search_keyword=specialty_kw,
                        insurance_sector=sector,
                        sweep_start_time=sweep_start,
                    )

            export_scope_samples(
                storage=storage,
                samples_dir=samples_dir,
                city_slug=city_key,
                specialty_slug=specialty_kw,
                sniper_max_days=args.sniper_days,
            )

    export_global_summary(
        storage=storage,
        samples_dir=samples_dir,
        sniper_max_days=args.sniper_days,
    )

    console.print(
        f"\n[bold green]Sweep Completed:[/bold green] "
        f"{total_providers_seen} provider records | "
        f"{total_snapshots} live calendars checked | "
        f"{total_sniper_matches} sniper intents generated.\n"
    )
    render_summary_tables(storage, sniper_days=args.sniper_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
