"""Live smoke test hitting doctolib.de with dynamic CSRF warm-up and anti-cache headers."""

from __future__ import annotations

import pytest

from src.core.client import ResilientHttpClient
from src.extractors.doctolib import DoctolibExtractor


@pytest.mark.live
def test_live_doctolib_chained_pipeline() -> None:
    with ResilientHttpClient(min_delay=0.1, max_delay=0.2) as client:
        csrf = client.warmup()
        assert csrf is not None and len(csrf) > 16

        extractor = DoctolibExtractor(client)
        search_resp = extractor.search_page(
            keyword="kardiologie",
            city_key="berlin",
            insurance_sector="private",
            page=0,
        )
        assert search_resp.total > 0
        assert len(search_resp.healthcare_providers) > 0

        bookable = next(
            (p for p in search_resp.healthcare_providers if p.is_bookable),
            None,
        )
        assert bookable is not None

        avail = extractor.fetch_provider_availability(
            provider=bookable,
            insurance_sector="private",
            limit_days=7,
        )
        assert avail is not None
        assert avail.profile_id == bookable.profile_id

        enriched = extractor.fetch_enriched_profile(
            profile_slug="mithal-nassar-berlin",
            practice_id=58464,
        )
        assert enriched.profile_id > 0
        assert "Nassar" in enriched.name_with_title
