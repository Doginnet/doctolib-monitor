"""Multi-stage Doctolib Germany API extractor chaining SEO taxonomy, HCP search, live slots, and deep profiles."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timezone

from loguru import logger

from config.cities import resolve_city
from src.core.client import ResilientHttpClient
from src.models.availability import AvailabilityPayload, ComputedAvailability
from src.models.profile import EnrichedDoctorProfile
from src.models.search import (
    HcpSearchResponse,
    HealthcareProviderCard,
    SeoSpecialtyLink,
)


class DoctolibExtractor:
    """Orchestrates Doctolib Germany's 4 chained JSON endpoints over ResilientHttpClient."""

    def __init__(self, client: ResilientHttpClient) -> None:
        self.client = client

    def fetch_seo_specialties(self) -> dict[str, list[SeoSpecialtyLink]]:
        """Endpoint #1: GET /seo_patient/seo_speciality_links."""
        raw = self.client.request_json("GET", "/seo_patient/seo_speciality_links")
        catalog: dict[str, list[SeoSpecialtyLink]] = {}
        if isinstance(raw, dict):
            for city_name, items in raw.items():
                if isinstance(items, list):
                    catalog[city_name] = [
                        SeoSpecialtyLink.model_validate(entry) for entry in items
                    ]
        return catalog

    def search_page(
        self,
        keyword: str,
        city_key: str,
        insurance_sector: str = "public",
        page: int = 0,
    ) -> HcpSearchResponse:
        """Endpoint #2: POST /patient-health-search/api/v1/hcp/search?page={page} (HTTP 206)."""
        city = resolve_city(city_key)
        sector = insurance_sector.strip().lower()
        payload = {
            "keyword": keyword.strip().lower(),
            "location": {"place": city.to_search_place()},
            "filters": {"insuranceSector": sector},
        }
        referer = (
            f"/search?insuranceSector={sector}"
            f"&keyword={keyword.strip().lower()}&location={city.slug}"
        )
        raw = self.client.request_json(
            "POST",
            "/patient-health-search/api/v1/hcp/search",
            params={"page": page},
            json_body=payload,
            referer_path=referer,
        )
        return HcpSearchResponse.model_validate(raw)

    def iter_providers(
        self,
        keyword: str,
        city_key: str,
        insurance_sector: str = "public",
        max_doctors: int | None = None,
    ) -> Iterator[HealthcareProviderCard]:
        """Paginates HCP directory results with deduplication and polite rate limiting."""
        page = 0
        yielded = 0
        total_reported: int | None = None
        seen_keys: set[tuple[int, int]] = set()

        while True:
            resp = self.search_page(
                keyword=keyword,
                city_key=city_key,
                insurance_sector=insurance_sector,
                page=page,
            )
            if total_reported is None:
                total_reported = resp.total
                logger.info(
                    f"[{city_key.upper()} | {keyword} | {insurance_sector.upper()}] "
                    f"Directory reports total={total_reported} providers"
                )

            if not resp.healthcare_providers:
                break

            new_on_page = 0
            for card in resp.healthcare_providers:
                dedup_key = (card.profile_id, card.practice_id)
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                new_on_page += 1
                yielded += 1
                yield card

                if max_doctors is not None and yielded >= max_doctors:
                    return

            if new_on_page == 0 or (total_reported is not None and len(seen_keys) >= total_reported):
                break

            page += 1
            self.client.polite_sleep()

    def fetch_provider_availability(
        self,
        provider: HealthcareProviderCard,
        insurance_sector: str = "public",
        start_date: date | None = None,
        limit_days: int = 15,
    ) -> ComputedAvailability | None:
        """Endpoint #3: GET /availabilities.json chained from provider's matchedVisitMotive."""
        motive = provider.matched_visit_motive
        agenda_ids = provider.effective_agenda_ids
        if not motive or not motive.visit_motive_id or not agenda_ids:
            return None

        effective_sector = insurance_sector.strip().lower()
        if motive.insurance_sector and motive.insurance_sector.normalized_sector in ("public", "private"):
            effective_sector = motive.insurance_sector.normalized_sector

        query_date = (start_date or datetime.now(timezone.utc).date()).isoformat()
        params = {
            "visit_motive_ids": str(motive.visit_motive_id),
            "agenda_ids": "-".join(str(aid) for aid in agenda_ids),
            "practice_ids": str(provider.practice_id),
            "insurance_sector": effective_sector,
            "telehealth": str(provider.is_telehealth).lower(),
            "start_date": query_date,
            "limit": limit_days,
        }
        spec_slug = provider.speciality.slug or "arzt"
        raw = self.client.request_json(
            "GET",
            "/availabilities.json",
            params=params,
            referer_path=f"/{spec_slug}/{provider.profile_slug}",
        )
        avail_payload = AvailabilityPayload.model_validate(raw)
        return avail_payload.compute_metrics(
            profile_id=provider.profile_id,
            practice_id=provider.practice_id,
            visit_motive_id=motive.visit_motive_id,
            motive_name=motive.name or "",
            insurance_sector=effective_sector,
            allow_new_patients=True if motive.allow_new_patients is None else motive.allow_new_patients,
        )

    def fetch_enriched_profile(
        self,
        profile_slug: str,
        practice_id: int | None = None,
        locale: str = "de",
    ) -> EnrichedDoctorProfile:
        """Endpoint #4: GET /profiles/{slug}.json?pid=practice-{id}&locale=de for phone & hours enrichment."""
        params: dict[str, str] = {"locale": locale}
        if practice_id:
            params["pid"] = f"practice-{practice_id}"

        raw = self.client.request_json(
            "GET",
            f"/profiles/{profile_slug}.json",
            params=params,
            referer_path=f"/profiles/{profile_slug}",
        )
        return EnrichedDoctorProfile.from_api_response(raw)
