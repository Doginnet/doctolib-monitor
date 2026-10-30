"""Pydantic v2 data contracts for Doctolib Germany."""

from src.models.availability import AvailabilityDay, AvailabilityPayload, ComputedAvailability
from src.models.booking import BookingIntentPayload, SniperRule
from src.models.profile import EnrichedDoctorProfile, PracticeEnrichment
from src.models.search import (
    GeoLocation,
    HealthcareProviderCard,
    HcpSearchResponse,
    MatchedVisitMotive,
    SeoSpecialtyLink,
)

__all__ = [
    "AvailabilityDay",
    "AvailabilityPayload",
    "BookingIntentPayload",
    "ComputedAvailability",
    "EnrichedDoctorProfile",
    "GeoLocation",
    "HcpSearchResponse",
    "HealthcareProviderCard",
    "MatchedVisitMotive",
    "PracticeEnrichment",
    "SeoSpecialtyLink",
    "SniperRule",
]
