"""Pydantic v2 schemas for Doctolib deep profile enrichment (/profiles/{slug}.json)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from src.models.search import normalize_doctolib_url


DAY_LABELS_DE = {
    1: "Mo",
    2: "Di",
    3: "Mi",
    4: "Do",
    5: "Fr",
    6: "Sa",
    7: "So",
}


class PracticeEnrichment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    practice_id: int
    formal_name: str = ""
    full_address: str = ""
    city: str = ""
    zipcode: str = ""
    latitude: float | None = None
    longitude: float | None = None
    landline_number: str | None = None
    opening_hours: list[str] = Field(default_factory=list)
    transit_stations: list[str] = Field(default_factory=list)


class EnrichedDoctorProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    profile_id: int
    slug: str
    name_with_title: str
    profile_url: HttpUrl
    website_url: HttpUrl | None = None
    specialty_name: str = ""
    specialty_slug: str = ""
    telehealth: bool = False
    languages: list[str] = Field(default_factory=list)
    procedures: list[str] = Field(default_factory=list)
    regulation_sector: str | None = None
    practices: list[PracticeEnrichment] = Field(default_factory=list)

    @classmethod
    def from_api_response(cls, payload: dict[str, Any]) -> EnrichedDoctorProfile:
        data = payload.get("data") or {}
        prof = data.get("profile") or {}
        places = data.get("places") or []
        details = data.get("details") or []

        raw_website = prof.get("website_url")
        norm_website_str = normalize_doctolib_url(raw_website) if raw_website else None
        website_url: HttpUrl | None = None
        if norm_website_str:
            try:
                website_url = HttpUrl(norm_website_str)
            except Exception:
                website_url = None

        profile_path = prof.get("profile_path") or f"/{prof.get('slug', '')}"
        profile_url = HttpUrl(
            normalize_doctolib_url(profile_path) or "https://www.doctolib.de"
        )

        languages = [
            item.get("iso_code", "").strip()
            for item in (prof.get("languages") or [])
            if isinstance(item, dict) and item.get("iso_code")
        ]

        procedures: list[str] = []
        skills_map = prof.get("skills_by_practice") or {}
        if isinstance(skills_map, dict):
            for skill_list in skills_map.values():
                if isinstance(skill_list, list):
                    for sk in skill_list:
                        if isinstance(sk, dict) and sk.get("name"):
                            name_val = str(sk["name"]).strip()
                            if name_val not in procedures:
                                procedures.append(name_val)

        reg_sector = None
        if details and isinstance(details[0], dict):
            reg_sector = details[0].get("regulation_sector")

        enriched_practices: list[PracticeEnrichment] = []
        for pl in places:
            if not isinstance(pl, dict):
                continue
            p_ids = pl.get("practice_ids") or []
            raw_pid = str(pl.get("id", "")).replace("practice-", "")
            pid = p_ids[0] if p_ids else (int(raw_pid) if raw_pid.isdigit() else 0)

            hours_formatted: list[str] = []
            for oh in pl.get("opening_hours") or []:
                if isinstance(oh, dict) and oh.get("enabled"):
                    day_lbl = DAY_LABELS_DE.get(oh.get("day", 0), f"D{oh.get('day')}")
                    ranges = [
                        f"{rng[0]}-{rng[1]}"
                        for rng in (oh.get("ranges") or [])
                        if isinstance(rng, list) and len(rng) == 2
                    ]
                    if ranges:
                        hours_formatted.append(f"{day_lbl}: {', '.join(ranges)}")

            stations_formatted: list[str] = []
            for st in pl.get("stations") or []:
                if isinstance(st, dict) and st.get("name"):
                    lines = "/".join(st.get("lines") or [])
                    stations_formatted.append(
                        f"{st['name']} ({lines})" if lines else str(st["name"])
                    )

            enriched_practices.append(
                PracticeEnrichment(
                    practice_id=pid,
                    formal_name=str(pl.get("formal_name") or pl.get("name") or "").strip(),
                    full_address=str(pl.get("full_address") or pl.get("address") or "").strip(),
                    city=str(pl.get("city") or "").strip(),
                    zipcode=str(pl.get("zipcode") or "").strip(),
                    latitude=pl.get("latitude"),
                    longitude=pl.get("longitude"),
                    landline_number=(pl.get("landline_number") or None),
                    opening_hours=hours_formatted,
                    transit_stations=stations_formatted,
                )
            )

        spec = prof.get("speciality") or {}
        return cls(
            profile_id=int(prof.get("id") or 0),
            slug=str(prof.get("slug") or ""),
            name_with_title=str(
                prof.get("name_with_title") or prof.get("name") or ""
            ).strip(),
            profile_url=profile_url,
            website_url=website_url,
            specialty_name=str(spec.get("name") or ""),
            specialty_slug=str(spec.get("slug") or ""),
            telehealth=bool(prof.get("telehealth")),
            languages=languages,
            procedures=procedures,
            regulation_sector=reg_sector,
            practices=enriched_practices,
        )

    @property
    def primary_phone(self) -> str | None:
        for p in self.practices:
            if p.landline_number:
                return p.landline_number
        return None

    @property
    def primary_opening_hours(self) -> list[str]:
        for p in self.practices:
            if p.opening_hours:
                return p.opening_hours
        return []
