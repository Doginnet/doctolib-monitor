"""Unified Doctolib sweep execution engine shared by Rich CLI and FastAPI web server."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_validator

from config.cities import DEFAULT_CI_SWEEP_TARGETS, resolve_city
from config.settings import settings
from src.alerts.dispatcher import AlertDispatcher
from src.booking.automator import DryRunBookingAutomator
from src.core.client import ResilientHttpClient
from src.extractors.doctolib import DoctolibExtractor
from src.models.booking import SniperRule
from src.pipelines.export import export_global_summary, export_scope_samples
from src.pipelines.storage import DoctolibStorage


class SweepRunRequest(BaseModel):
    """Validated configuration contract for launching a Doctolib Germany scrape sweep."""

    model_config = ConfigDict(extra="ignore")

    # Target & Pagination Parameters
    city: str = Field(default="berlin", min_length=2)
    specialty: str = Field(default="kardiologie", min_length=2)
    insurance: Literal["public", "private", "both"] = "both"
    max_doctors: int = Field(default=20, ge=1, le=500)
    limit_days: int = Field(default=15, ge=1, le=15)

    # Sniper & Enrichment Parameters
    sniper_days: float = Field(default=7.0, ge=0.0, le=365.0)
    max_enrichments: int = Field(default=8, ge=0, le=100)
    preferred_hour_start: int = Field(default=8, ge=0, le=23)
    preferred_hour_end: int = Field(default=19, ge=0, le=23)

    # Network & Runtime Parameters
    min_delay: float = Field(default=0.35, ge=0.0, le=10.0)
    max_delay: float = Field(default=0.85, ge=0.0, le=20.0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    webhook_url_override: str = Field(default="")

    # Boolean Checkbox Flags
    ci_sweep: bool = False
    scrape_all: bool = False
    auto_book_mock: bool = True
    notify: bool = False
    allow_new_patients_only: bool = True
    require_telehealth: bool = False

    @model_validator(mode="after")
    def _validate_consistency(self) -> SweepRunRequest:
        if not self.ci_sweep:
            resolve_city(self.city)
        if self.preferred_hour_end < self.preferred_hour_start:
            self.preferred_hour_end = self.preferred_hour_start
        if self.max_delay < self.min_delay:
            self.max_delay = self.min_delay
        return self

    def to_cli_command(self) -> str:
        """Builds the equivalent CLI invocation string for UI display and reproducibility."""
        parts = ["uv run python -m src.main"]
        if self.ci_sweep:
            parts.append("--ci-sweep")
        else:
            parts.append(f"--city {self.city.strip().lower()}")
            parts.append(f"--specialty {self.specialty.strip().lower()}")
        parts.append(f"--insurance {self.insurance}")
        if self.scrape_all:
            parts.append("--all")
        else:
            parts.append(f"--max-doctors {self.max_doctors}")
        parts.append(f"--sniper-days {self.sniper_days}")
        parts.append(f"--max-enrichments {self.max_enrichments}")
        if self.auto_book_mock:
            parts.append("--auto-book-mock")
        if self.notify:
            parts.append("--notify")
        if self.log_level != "INFO":
            parts.append(f"--log-level {self.log_level}")
        return " ".join(parts)


class SweepRunSummary(BaseModel):
    status: Literal["completed", "stopped", "failed"]
    providers_seen: int = 0
    calendars_checked: int = 0
    sniper_matches: int = 0
    enrichments_done: int = 0
    alerts_dispatched: int = 0
    elapsed_seconds: float = 0.0
    alert_mode: str = "SIMULATION"
    error_message: str | None = None


def execute_doctolib_sweep(
    config: SweepRunRequest,
    *,
    db_path: Path | None = None,
    samples_dir: Path | None = None,
    stop_event: threading.Event | None = None,
    on_log: Callable[[str, str], None] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> SweepRunSummary:
    """Runs the 4-stage Doctolib Germany sweep synchronously in the caller's thread."""
    t0 = time.monotonic()
    resolved_db = db_path or settings.resolved_db_path()
    resolved_samples = samples_dir or settings.resolved_samples_dir()

    storage = DoctolibStorage(resolved_db)
    dispatcher = AlertDispatcher(
        webhook_url=config.webhook_url_override.strip() or None,
    )
    alert_mode = "SIMULATION" if dispatcher.is_mock_mode else "LIVE_WEBHOOKS"

    def emit_log(level: str, msg: str) -> None:
        log_fn = getattr(logger, level.lower(), logger.info)
        log_fn(msg)
        if on_log is not None:
            on_log(level.upper(), msg)

    targets: list[tuple[str, str]] = (
        list(DEFAULT_CI_SWEEP_TARGETS)
        if config.ci_sweep
        else [(resolve_city(config.city).slug, config.specialty.strip().lower())]
    )
    sectors = ["public", "private"] if config.insurance == "both" else [config.insurance]
    max_docs = None if config.scrape_all else config.max_doctors

    total_scopes = len(targets) * len(sectors)
    completed_scopes = 0
    providers_seen = 0
    calendars_checked = 0
    sniper_matches = 0
    total_enrichments = 0
    alerts_dispatched = 0
    stopped = False

    def push_progress(current_scope: str) -> None:
        if on_progress is not None:
            on_progress(
                {
                    "current_scope": current_scope,
                    "completed_scopes": completed_scopes,
                    "total_scopes": total_scopes,
                    "providers_seen": providers_seen,
                    "calendars_checked": calendars_checked,
                    "sniper_matches": sniper_matches,
                    "enrichments_done": total_enrichments,
                    "alerts_dispatched": alerts_dispatched,
                    "alert_mode": alert_mode,
                    "elapsed_seconds": round(time.monotonic() - t0, 1),
                }
            )

    emit_log(
        "INFO",
        f"Starting Doctolib sweep: {len(targets)} target(s), sectors={','.join(sectors).upper()}, "
        f"sniper<={config.sniper_days}d, mode={alert_mode}",
    )
    push_progress("Warming up session & CSRF token...")

    try:
        with ResilientHttpClient(
            min_delay=config.min_delay,
            max_delay=config.max_delay,
        ) as client:
            csrf = client.warmup()
            emit_log(
                "INFO",
                f"Session warm-up OK (Chrome124 TLS, CSRF={'present' if csrf else 'cookie-only'})",
            )
            extractor = DoctolibExtractor(client)

            for city_key, specialty_kw in targets:
                if stop_event and stop_event.is_set():
                    stopped = True
                    break

                enrichments_in_city = 0

                for sector in sectors:
                    if stop_event and stop_event.is_set():
                        stopped = True
                        break

                    scope_label = f"{city_key.upper()} / {specialty_kw} / {sector.upper()}"
                    push_progress(scope_label)
                    emit_log("INFO", f"Scanning directory scope: {scope_label}")

                    sweep_start = datetime.now(timezone.utc)
                    rule = SniperRule(
                        max_wait_days=config.sniper_days,
                        insurance_sector=sector,
                        allow_new_patients_only=config.allow_new_patients_only,
                        preferred_hour_start=config.preferred_hour_start,
                        preferred_hour_end=config.preferred_hour_end,
                        require_telehealth=config.require_telehealth,
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
                        if stop_event and stop_event.is_set():
                            stopped = True
                            emit_log("WARNING", "Stop signal received — halting sweep gracefully.")
                            break

                        storage.upsert_provider(
                            card=card,
                            city_key=city_key,
                            search_keyword=specialty_kw,
                            insurance_sector=sector,
                        )
                        providers_seen += 1

                        if not card.is_bookable:
                            push_progress(scope_label)
                            continue

                        try:
                            avail = extractor.fetch_provider_availability(
                                provider=card,
                                insurance_sector=sector,
                                limit_days=config.limit_days,
                            )
                        except Exception as avail_err:
                            emit_log(
                                "WARNING",
                                f"Availability check skipped for {card.profile_slug}: {avail_err}",
                            )
                            continue

                        if avail is None:
                            push_progress(scope_label)
                            continue

                        storage.insert_slot_snapshot(avail)
                        calendars_checked += 1

                        if avail.wait_time_days is not None:
                            slot_preview = (avail.next_slot_iso or "")[:16].replace("T", " ")
                            emit_log(
                                "DEBUG",
                                f"{card.full_display_name} ({sector.upper()}): "
                                f"earliest={slot_preview} ({avail.wait_time_days}d)",
                            )

                        if config.auto_book_mock or config.notify:
                            should_enrich = enrichments_in_city < config.max_enrichments
                            intent = automator.evaluate_and_reserve_mock(
                                provider=card,
                                avail=avail,
                                enrich_profile=should_enrich,
                            )
                            if intent is not None:
                                sniper_matches += 1
                                if should_enrich:
                                    enrichments_in_city += 1
                                    total_enrichments += 1
                                emit_log(
                                    "INFO",
                                    f"SNIPER MATCH: {intent.doctor_name} ({sector.upper()}) @ "
                                    f"{intent.target_slot_iso[:16].replace('T', ' ')} "
                                    f"(wait {intent.wait_time_days}d)",
                                )
                                if config.notify:
                                    dispatcher.dispatch(intent)
                                    alerts_dispatched += 1

                        push_progress(scope_label)
                        client.polite_sleep()

                    if config.scrape_all and not stopped:
                        deactivated = storage.deactivate_unseen_providers(
                            city_key=city_key,
                            search_keyword=specialty_kw,
                            insurance_sector=sector,
                            sweep_start_time=sweep_start,
                        )
                        if deactivated:
                            emit_log(
                                "INFO",
                                f"Soft-deactivated {deactivated} stale records in {scope_label}",
                            )

                    completed_scopes += 1
                    push_progress(scope_label)

                export_scope_samples(
                    storage=storage,
                    samples_dir=resolved_samples,
                    city_slug=city_key,
                    specialty_slug=specialty_kw,
                    sniper_max_days=config.sniper_days,
                )

        export_global_summary(
            storage=storage,
            samples_dir=resolved_samples,
            sniper_max_days=config.sniper_days,
        )

        elapsed = round(time.monotonic() - t0, 2)
        final_status: Literal["completed", "stopped"] = "stopped" if stopped else "completed"
        emit_log(
            "INFO",
            f"Sweep {final_status.upper()} in {elapsed}s — providers={providers_seen}, "
            f"calendars={calendars_checked}, sniper_matches={sniper_matches}",
        )
        push_progress("Idle")
        return SweepRunSummary(
            status=final_status,
            providers_seen=providers_seen,
            calendars_checked=calendars_checked,
            sniper_matches=sniper_matches,
            enrichments_done=total_enrichments,
            alerts_dispatched=alerts_dispatched,
            elapsed_seconds=elapsed,
            alert_mode=alert_mode,
        )

    except Exception as exc:
        elapsed = round(time.monotonic() - t0, 2)
        emit_log("ERROR", f"Sweep failed after {elapsed}s: {exc}")
        return SweepRunSummary(
            status="failed",
            providers_seen=providers_seen,
            calendars_checked=calendars_checked,
            sniper_matches=sniper_matches,
            enrichments_done=total_enrichments,
            alerts_dispatched=alerts_dispatched,
            elapsed_seconds=elapsed,
            alert_mode=alert_mode,
            error_message=str(exc),
        )
