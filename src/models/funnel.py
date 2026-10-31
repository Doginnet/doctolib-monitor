"""Pydantic v2 models and URL parser for Doctolib's live booking funnel (/online_booking/api/slot_selection_funnel/v1/info.json)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, Field


PRACTICE_PID_RE = re.compile(r"practice-(\d+)", re.I)


def extract_doctolib_slug_and_pid(url_or_slug: str) -> tuple[str, int | None]:
    """
    Extracts `(profile_slug, practice_id)` from any Doctolib Germany profile/booking URL or raw slug.
    Examples:
      - https://www.doctolib.de/kardiologie/berlin/anne-winkelmann?pid=practice-544676 -> ('anne-winkelmann', 544676)
      - https://www.doctolib.de/profiles/mithal-nassar-berlin -> ('mithal-nassar-berlin', None)
      - anne-winkelmann -> ('anne-winkelmann', None)
    """
    cleaned = url_or_slug.strip()
    if not cleaned:
        raise ValueError("Empty Doctolib URL or profile slug.")

    if "doctolib." not in cleaned.lower() and "/" not in cleaned:
        slug_only = cleaned.split("?")[0].strip().lower()
        return slug_only, None

    if not cleaned.startswith(("http://", "https://")):
        cleaned = f"https://{cleaned.lstrip('/')}"

    parsed = urlparse(cleaned)
    host = parsed.netloc.lower()
    if host and "doctolib." not in host:
        raise ValueError(f"URL does not belong to Doctolib: {parsed.netloc}")

    segments = [seg.strip() for seg in parsed.path.split("/") if seg.strip()]
    # Filter out trailing action segments like /booking or .json
    filtered_segments = [
        seg.removesuffix(".json")
        for seg in segments
        if seg.lower() not in ("booking", "availabilities", "info.json")
    ]
    if not filtered_segments:
        raise ValueError(f"Could not extract doctor profile slug from URL: {url_or_slug}")

    profile_slug = filtered_segments[-1].lower()
    practice_id: int | None = None

    qs = parse_qs(parsed.query)
    for pid_candidate in qs.get("pid", []):
        m = PRACTICE_PID_RE.search(pid_candidate)
        if m:
            practice_id = int(m.group(1))
            break
        if pid_candidate.isdigit():
            practice_id = int(pid_candidate)
            break

    return profile_slug, practice_id


class DoctorProcedureOption(BaseModel):
    """Represents a bookable procedure (visit_motive) and its linked agendas for a specific doctor."""

    model_config = ConfigDict(extra="ignore")

    visit_motive_id: int
    name: str
    telehealth: bool = False
    practice_id: int
    agenda_ids: list[int] = Field(default_factory=list)


class BookingFunnelInfo(BaseModel):
    """Normalized doctor profile and bookable procedure catalog for Telegram Bot Mode 2."""

    model_config = ConfigDict(extra="ignore")

    profile_id: int
    profile_slug: str
    doctor_name: str
    specialty_name: str = "Facharzt"
    specialty_slug: str = "arzt"
    city: str = ""
    address: str = ""
    landline_number: str | None = None
    default_practice_id: int = 0
    booking_url: str
    procedures: list[DoctorProcedureOption] = Field(default_factory=list)

    @classmethod
    def from_api_response(
        cls,
        payload: dict[str, Any],
        fallback_slug: str = "",
        preferred_practice_id: int | None = None,
    ) -> BookingFunnelInfo:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        prof = data.get("profile") or {}
        places = data.get("places") or []
        motives = data.get("visit_motives") or []
        agendas = data.get("agendas") or []

        slug = str(prof.get("slug") or fallback_slug).strip()
        profile_id = int(prof.get("id") or 0)
        doctor_name = str(prof.get("name_with_title") or prof.get("name") or slug).strip()
        spec = prof.get("speciality") or {}
        spec_name = str(spec.get("name") or "Facharzt").strip()
        spec_slug = str(spec.get("slug") or "arzt").strip()

        chosen_place: dict[str, Any] = {}
        default_pid = preferred_practice_id or 0
        for pl in places:
            if not isinstance(pl, dict):
                continue
            pids = pl.get("practice_ids") or []
            raw_id = str(pl.get("id") or "").replace("practice-", "")
            pl_pid = int(pids[0]) if pids else (int(raw_id) if raw_id.isdigit() else 0)
            if preferred_practice_id and pl_pid == preferred_practice_id:
                chosen_place = pl
                default_pid = pl_pid
                break
            if not chosen_place:
                chosen_place = pl
                if not default_pid:
                    default_pid = pl_pid

        city = str(chosen_place.get("city") or "").strip()
        addr_parts = [
            str(chosen_place.get("address") or "").strip(),
            f"{chosen_place.get('zipcode') or ''} {city}".strip(),
        ]
        address = ", ".join(p for p in addr_parts if p)
        phone = chosen_place.get("landline_number") or None

        active_agendas = [
            ag
            for ag in agendas
            if isinstance(ag, dict) and not ag.get("booking_temporary_disabled", False)
        ]

        procedures: list[DoctorProcedureOption] = []
        for mv in motives:
            if not isinstance(mv, dict) or not mv.get("id") or not mv.get("name"):
                continue
            motive_id = int(mv["id"])
            matched_agendas: list[int] = []
            matched_pid: int = default_pid

            for ag in active_agendas:
                ag_id = ag.get("id")
                ag_pid = int(ag.get("practice_id") or 0)
                if preferred_practice_id and ag_pid and ag_pid != preferred_practice_id:
                    continue

                vm_list = ag.get("visit_motive_ids") or []
                by_prac = ag.get("visit_motive_ids_by_practice_id") or {}
                in_by_prac = any(
                    isinstance(vlist, list) and motive_id in vlist for vlist in by_prac.values()
                )
                if motive_id in vm_list or in_by_prac:
                    if ag_id and int(ag_id) not in matched_agendas:
                        matched_agendas.append(int(ag_id))
                    if not matched_pid and ag_pid:
                        matched_pid = ag_pid

            if matched_agendas:
                procedures.append(
                    DoctorProcedureOption(
                        visit_motive_id=motive_id,
                        name=str(mv["name"]).strip(),
                        telehealth=bool(mv.get("telehealth", False)),
                        practice_id=matched_pid,
                        agenda_ids=matched_agendas,
                    )
                )

        city_path = city.lower().replace("ü", "ue").replace("ö", "oe").replace("ä", "ae") or "deutschland"
        pid_query = f"?pid=practice-{default_pid}" if default_pid else ""
        booking_url = f"https://www.doctolib.de/{spec_slug}/{city_path}/{slug}{pid_query}"

        return cls(
            profile_id=profile_id,
            profile_slug=slug,
            doctor_name=doctor_name,
            specialty_name=spec_name,
            specialty_slug=spec_slug,
            city=city,
            address=address,
            landline_number=phone,
            default_practice_id=default_pid,
            booking_url=booking_url,
            procedures=procedures,
        )
