"""Verified German city bounding-box presets and high-demand medical specialties for doctolib.de."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CityPreset:
    slug: str
    name: str
    lat: float
    lng: float
    ne_lat: float
    ne_lng: float
    sw_lat: float
    sw_lng: float
    country: str = "de"

    def to_search_place(self) -> dict[str, Any]:
        """Builds the exact location.place payload accepted by POST /patient-health-search/api/v1/hcp/search."""
        return {
            "name": self.name,
            "nameWithPronoun": f"in {self.name} ",
            "slug": self.slug,
            "country": self.country,
            "type": "locality",
            "locality": self.name,
            "administrativeAreaLevel1": self.name,
            "gpsPoint": {"lat": self.lat, "lng": self.lng},
            "viewport": {
                "northeast": {"lat": self.ne_lat, "lng": self.ne_lng},
                "southwest": {"lat": self.sw_lat, "lng": self.sw_lng},
            },
        }


GERMAN_CITIES: dict[str, CityPreset] = {
    "berlin": CityPreset(
        slug="berlin",
        name="Berlin",
        lat=52.5200066,
        lng=13.4049540,
        ne_lat=52.6754542,
        ne_lng=13.7611175,
        sw_lat=52.3396296,
        sw_lng=13.0911720,
    ),
    "muenchen": CityPreset(
        slug="muenchen",
        name="München",
        lat=48.1351253,
        lng=11.5819806,
        ne_lat=48.2482197,
        ne_lng=11.7228755,
        sw_lat=48.0616018,
        sw_lng=11.3607960,
    ),
    "hamburg": CityPreset(
        slug="hamburg",
        name="Hamburg",
        lat=53.5510846,
        lng=9.9936819,
        ne_lat=53.7394336,
        ne_lng=10.3252805,
        sw_lat=53.3951118,
        sw_lng=9.7301319,
    ),
    "koeln": CityPreset(
        slug="koeln",
        name="Köln",
        lat=50.9375310,
        lng=6.9602786,
        ne_lat=51.0849743,
        ne_lng=7.1620280,
        sw_lat=50.8304399,
        sw_lng=6.7725303,
    ),
    "frankfurt": CityPreset(
        slug="frankfurt",
        name="Frankfurt am Main",
        lat=50.1109221,
        lng=8.6821267,
        ne_lat=50.2269512,
        ne_lng=8.8004960,
        sw_lat=50.0155200,
        sw_lng=8.4727150,
    ),
    "stuttgart": CityPreset(
        slug="stuttgart",
        name="Stuttgart",
        lat=48.7758459,
        lng=9.1829321,
        ne_lat=48.8663994,
        ne_lng=9.3160229,
        sw_lat=48.6920188,
        sw_lng=9.0386007,
    ),
    "duesseldorf": CityPreset(
        slug="duesseldorf",
        name="Düsseldorf",
        lat=51.2277411,
        lng=6.7734556,
        ne_lat=51.3525190,
        ne_lng=6.9398833,
        sw_lat=51.1243572,
        sw_lng=6.6888154,
    ),
    "leipzig": CityPreset(
        slug="leipzig",
        name="Leipzig",
        lat=51.3396955,
        lng=12.3730747,
        ne_lat=51.4480260,
        ne_lng=12.5483571,
        sw_lat=51.2381987,
        sw_lng=12.2366320,
    ),
}


MEDICAL_SPECIALTIES: dict[str, str] = {
    "hautarzt": "Dermatologie (Hautarzt)",
    "kardiologie": "Kardiologie",
    "augenheilkunde": "Augenheilkunde (Augenarzt)",
    "radiologe": "Radiologie (MRT / CT)",
    "orthopadie": "Orthopädie & Unfallchirurgie",
    "neurologie": "Neurologie",
    "allgemeinmedizin": "Allgemeinmedizin (Hausarzt)",
    "urologie": "Urologie",
    "frauenarzt": "Gynäkologie (Frauenarzt)",
    "zahnmedizin": "Zahnmedizin (Zahnarzt)",
    "facharzt-fur-hno": "HNO-Heilkunde",
    "ct-kardio-herz": "CT Kardio / Herz",
}


DEFAULT_CI_SWEEP_TARGETS: list[tuple[str, str]] = [
    ("berlin", "hautarzt"),
    ("berlin", "kardiologie"),
    ("berlin", "augenheilkunde"),
    ("muenchen", "hautarzt"),
    ("muenchen", "kardiologie"),
    ("hamburg", "kardiologie"),
]


def resolve_city(city_key: str) -> CityPreset:
    normalized = (
        city_key.strip()
        .lower()
        .replace("ü", "ue")
        .replace("ö", "oe")
        .replace("ä", "ae")
        .replace("ß", "ss")
    )
    if normalized in GERMAN_CITIES:
        return GERMAN_CITIES[normalized]
    raise ValueError(
        f"Unsupported city '{city_key}'. Supported presets: {', '.join(GERMAN_CITIES.keys())}"
    )
