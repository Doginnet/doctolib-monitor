"""FastAPI server providing the primary HTML launcher UI, background job runner, and JSON/CSV data APIs."""

from __future__ import annotations

import csv
import io
import json
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from config.cities import DEFAULT_CI_SWEEP_TARGETS, GERMAN_CITIES, MEDICAL_SPECIALTIES
from config.settings import settings
from src.alerts.dispatcher import AlertDispatcher
from src.models.booking import BookingIntentPayload
from src.pipelines.export import CSV_COLUMNS
from src.pipelines.runner import SweepRunRequest, SweepRunSummary, execute_doctolib_sweep
from src.pipelines.storage import DoctolibStorage


STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_HTML_PATH = STATIC_DIR / "index.html"


class WebhookTestRequest(BaseModel):
    webhook_url: str = ""


class ScrapeJobManager:
    """Thread-safe background job controller with incremental log streaming."""

    def __init__(self, db_path: Path | None = None, samples_dir: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._db_path = db_path
        self._samples_dir = samples_dir
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._state: str = "idle"
        self._run_id: str | None = None
        self._started_at: str | None = None
        self._active_config: SweepRunRequest | None = None
        self._cli_command: str = ""
        self._progress: dict[str, Any] = {
            "current_scope": "Idle",
            "completed_scopes": 0,
            "total_scopes": 0,
            "providers_seen": 0,
            "calendars_checked": 0,
            "sniper_matches": 0,
            "enrichments_done": 0,
            "alerts_dispatched": 0,
            "alert_mode": "SIMULATION",
            "elapsed_seconds": 0.0,
        }
        self._last_summary: dict[str, Any] | None = None
        self._logs: deque[dict[str, Any]] = deque(maxlen=500)
        self._log_seq: int = 0

    @property
    def db_path(self) -> Path:
        return self._db_path or settings.resolved_db_path()

    @property
    def samples_dir(self) -> Path:
        return self._samples_dir or settings.resolved_samples_dir()

    def _append_log(self, level: str, message: str) -> None:
        with self._lock:
            self._log_seq += 1
            self._logs.append(
                {
                    "seq": self._log_seq,
                    "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                    "level": level,
                    "message": message,
                }
            )

    def _update_progress(self, stats: dict[str, Any]) -> None:
        with self._lock:
            self._progress.update(stats)

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start_job(self, config: SweepRunRequest) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise HTTPException(
                    status_code=409,
                    detail="A Doctolib scrape job is already running. Stop it or wait for completion.",
                )

            self._stop_event = threading.Event()
            self._run_id = uuid.uuid4().hex[:10]
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._state = "running"
            self._active_config = config
            self._cli_command = config.to_cli_command()
            self._logs.clear()
            self._log_seq = 0
            self._progress = {
                "current_scope": "Initializing...",
                "completed_scopes": 0,
                "total_scopes": (len(DEFAULT_CI_SWEEP_TARGETS) if config.ci_sweep else 1)
                * (2 if config.insurance == "both" else 1),
                "providers_seen": 0,
                "calendars_checked": 0,
                "sniper_matches": 0,
                "enrichments_done": 0,
                "alerts_dispatched": 0,
                "alert_mode": "SIMULATION",
                "elapsed_seconds": 0.0,
            }

            stop_evt = self._stop_event

            def _worker() -> None:
                summary: SweepRunSummary = execute_doctolib_sweep(
                    config,
                    db_path=self.db_path,
                    samples_dir=self.samples_dir,
                    stop_event=stop_evt,
                    on_log=self._append_log,
                    on_progress=self._update_progress,
                )
                with self._lock:
                    self._state = summary.status
                    self._last_summary = summary.model_dump()

            self._thread = threading.Thread(
                target=_worker,
                name=f"doctolib-sweep-{self._run_id}",
                daemon=True,
            )
            self._thread.start()

            return {
                "status": "started",
                "run_id": self._run_id,
                "cli_command": self._cli_command,
            }

    def request_stop(self) -> dict[str, Any]:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return {"status": "idle", "message": "No active sweep is running."}
            self._stop_event.set()
            return {"status": "stopping", "run_id": self._run_id}

    def get_status(self, since_seq: int = 0) -> dict[str, Any]:
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
            state = "running" if alive else self._state
            logs = [entry for entry in self._logs if entry["seq"] > since_seq]
            return {
                "state": state,
                "is_running": alive,
                "run_id": self._run_id,
                "started_at": self._started_at,
                "cli_command": self._cli_command,
                "active_config": self._active_config.model_dump() if self._active_config else None,
                "progress": dict(self._progress),
                "last_summary": self._last_summary,
                "latest_seq": self._log_seq,
                "logs": logs,
            }


def create_app(
    db_path: Path | None = None,
    samples_dir: Path | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Doctolib Germany Slot Sniper & Wait-Time Control Center",
        version="0.2.0",
        description="FastAPI HTML Control Center & REST Automation API for Doctolib Germany (doctolib.de)",
    )
    job_manager = ScrapeJobManager(db_path=db_path, samples_dir=samples_dir)
    app.state.job_manager = job_manager

    def _get_storage() -> DoctolibStorage:
        return DoctolibStorage(job_manager.db_path)

    @app.get("/", response_class=HTMLResponse)
    def serve_index() -> HTMLResponse:
        if not INDEX_HTML_PATH.exists():
            raise HTTPException(status_code=500, detail="Frontend index.html missing.")
        html = INDEX_HTML_PATH.read_text(encoding="utf-8")
        return HTMLResponse(
            content=html,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/api/health")
    def health_check() -> dict[str, Any]:
        return {
            "status": "ok",
            "is_running": job_manager.is_running(),
            "db_path": str(job_manager.db_path),
        }

    @app.get("/api/meta")
    def get_metadata() -> dict[str, Any]:
        dispatcher = AlertDispatcher()
        cities = [
            {
                "slug": preset.slug,
                "name": preset.name,
                "lat": preset.lat,
                "lng": preset.lng,
            }
            for preset in GERMAN_CITIES.values()
        ]
        specialties = [
            {"slug": slug, "label": label}
            for slug, label in MEDICAL_SPECIALTIES.items()
        ]
        return {
            "cities": cities,
            "specialties": specialties,
            "ci_sweep_targets": [
                {"city": c, "specialty": s} for c, s in DEFAULT_CI_SWEEP_TARGETS
            ],
            "defaults": {
                "city": "berlin",
                "specialty": "kardiologie",
                "insurance": "both",
                "max_doctors": 20,
                "limit_days": 15,
                "sniper_days": settings.sniper_max_wait_days,
                "max_enrichments": 8,
                "preferred_hour_start": settings.sniper_preferred_hour_start,
                "preferred_hour_end": settings.sniper_preferred_hour_end,
                "min_delay": settings.doctolib_min_delay,
                "max_delay": settings.doctolib_max_delay,
                "log_level": "INFO",
                "ci_sweep": False,
                "scrape_all": False,
                "auto_book_mock": True,
                "notify": False,
                "allow_new_patients_only": settings.sniper_allow_new_patients_only,
                "require_telehealth": False,
            },
            "webhooks": {
                "is_mock_mode": dispatcher.is_mock_mode,
                "has_webhook_url": bool(dispatcher.webhook_url),
                "has_slack": bool(dispatcher.slack_webhook_url),
                "has_telegram": bool(dispatcher.telegram_bot_token and dispatcher.telegram_chat_id),
            },
        }

    @app.post("/api/scrape/start")
    def start_scrape(payload: SweepRunRequest) -> dict[str, Any]:
        return job_manager.start_job(payload)

    @app.post("/api/scrape/stop")
    def stop_scrape() -> dict[str, Any]:
        return job_manager.request_stop()

    @app.get("/api/scrape/status")
    def scrape_status(since_seq: int = Query(default=0, ge=0)) -> dict[str, Any]:
        return job_manager.get_status(since_seq=since_seq)

    def _safe_json_list(raw: Any) -> list[Any]:
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
        return []

    @app.get("/api/data/overview")
    def data_overview(
        city: str | None = Query(default=None),
        specialty: str | None = Query(default=None),
        insurance: str | None = Query(default=None),
        max_wait_days: float | None = Query(default=None, ge=0.0),
        sniper_threshold: float = Query(default=7.0, ge=0.0),
        bookable_only: bool = Query(default=False),
        limit: int = Query(default=500, ge=1, le=2000),
    ) -> dict[str, Any]:
        storage = _get_storage()
        city_filter = city.strip().lower() if city and city != "all" else None
        spec_filter = specialty.strip().lower() if specialty and specialty != "all" else None
        ins_filter = insurance.strip().lower() if insurance and insurance != "all" else None

        raw_records = storage.get_latest_availability_records(
            city_slug=city_filter,
            specialty_slug=spec_filter,
            insurance_sector=ins_filter,
            max_wait_days=max_wait_days,
            active_only=True,
            bookable_only=bookable_only,
            limit=limit,
        )

        records: list[dict[str, Any]] = []
        pub_waits: list[float] = []
        priv_waits: list[float] = []
        urgent_count = 0
        bookable_count = 0

        for r in raw_records:
            wait = r.get("wait_time_days")
            sec = r.get("insurance_sector")
            if wait is not None:
                bookable_count += 1
                if wait <= sniper_threshold:
                    urgent_count += 1
                if sec == "public":
                    pub_waits.append(float(wait))
                elif sec == "private":
                    priv_waits.append(float(wait))

            item = dict(r)
            item["languages"] = _safe_json_list(r.get("languages_json"))
            item["sample_slots"] = _safe_json_list(r.get("sample_slots_json"))
            item["opening_hours"] = _safe_json_list(r.get("opening_hours_json"))
            records.append(item)

        pub_avg = round(sum(pub_waits) / len(pub_waits), 2) if pub_waits else None
        priv_avg = round(sum(priv_waits) / len(priv_waits), 2) if priv_waits else None
        multiplier = (
            round(pub_avg / priv_avg, 1)
            if (pub_avg is not None and priv_avg is not None and priv_avg > 0)
            else None
        )

        all_unfiltered = storage.get_latest_availability_records(active_only=True, limit=2000)
        distinct_cities = sorted(
            {(r["city_slug"], r["city_name"]) for r in all_unfiltered if r.get("city_slug")},
            key=lambda x: x[1] or x[0],
        )
        distinct_specs = sorted(
            {
                (r["specialty_slug"], r["specialty_name"])
                for r in all_unfiltered
                if r.get("specialty_slug")
            },
            key=lambda x: x[1] or x[0],
        )

        raw_intents = storage.get_booking_intents(limit=25)
        intents: list[dict[str, Any]] = []
        for bi in raw_intents:
            entry = dict(bi)
            try:
                entry["payload"] = json.loads(bi.get("payload_json") or "{}")
            except json.JSONDecodeError:
                entry["payload"] = {}
            intents.append(entry)

        return {
            "kpis": {
                "total_records": len(records),
                "bookable_records": bookable_count,
                "urgent_slots": urgent_count,
                "public_avg_wait": pub_avg,
                "private_avg_wait": priv_avg,
                "access_multiplier": multiplier,
            },
            "available_filters": {
                "cities": [{"slug": s, "name": n or s} for s, n in distinct_cities],
                "specialties": [{"slug": s, "name": n or s} for s, n in distinct_specs],
            },
            "records": records,
            "insurance_gap": storage.get_insurance_gap_summary(active_only=True),
            "booking_intents": intents,
        }

    @app.get("/api/data/export.csv")
    def download_csv(
        city: str | None = Query(default=None),
        specialty: str | None = Query(default=None),
        insurance: str | None = Query(default=None),
        max_wait_days: float | None = Query(default=None, ge=0.0),
        bookable_only: bool = Query(default=False),
    ) -> Response:
        storage = _get_storage()
        rows = storage.get_latest_availability_records(
            city_slug=city if city and city != "all" else None,
            specialty_slug=specialty if specialty and specialty != "all" else None,
            insurance_sector=insurance if insurance and insurance != "all" else None,
            max_wait_days=max_wait_days,
            active_only=True,
            bookable_only=bookable_only,
            limit=5000,
        )
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="doctolib_availability.csv"'},
        )

    @app.get("/api/data/export.json")
    def download_json(
        city: str | None = Query(default=None),
        specialty: str | None = Query(default=None),
        insurance: str | None = Query(default=None),
        max_wait_days: float | None = Query(default=None, ge=0.0),
        bookable_only: bool = Query(default=False),
    ) -> Response:
        storage = _get_storage()
        rows = storage.get_latest_availability_records(
            city_slug=city if city and city != "all" else None,
            specialty_slug=specialty if specialty and specialty != "all" else None,
            insurance_sector=insurance if insurance and insurance != "all" else None,
            max_wait_days=max_wait_days,
            active_only=True,
            bookable_only=bookable_only,
            limit=5000,
        )
        return Response(
            content=json.dumps(rows, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="doctolib_availability.json"'},
        )

    @app.post("/api/webhooks/test")
    def test_webhook(req: WebhookTestRequest) -> dict[str, Any]:
        storage = _get_storage()
        intents = storage.get_booking_intents(limit=1)
        if intents:
            sample_intent = BookingIntentPayload.model_validate_json(intents[0]["payload_json"])
        else:
            sample_intent = BookingIntentPayload(
                profile_id=100001,
                practice_id=269447,
                visit_motive_id=13898103,
                agenda_ids=[2176252],
                doctor_name="Dr. med. Beispiel Kardiologe",
                specialty="Kardiologie",
                city="Berlin",
                address="Friedrichstraße 100, 10117 Berlin",
                clinic_phone="030 12345678",
                opening_hours=["08:00 - 18:00"],
                insurance_sector="public",
                target_slot_iso=datetime.now(timezone.utc).isoformat(),
                wait_time_days=1.4,
                booking_url="https://www.doctolib.de/kardiologie/berlin",
            )

        dispatcher = AlertDispatcher(
            webhook_url=req.webhook_url.strip() or None,
        )
        delivery = dispatcher.dispatch(sample_intent)
        return {
            "mode": "SIMULATION" if dispatcher.is_mock_mode else "LIVE",
            "delivery": delivery,
            "payload": json.loads(sample_intent.model_dump_json()),
        }

    return app


app = create_app()


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    uvicorn.run("src.web.api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    run_server()
