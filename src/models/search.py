"""Pydantic v2 schemas for Doctolib SEO taxonomy and HCP directory search."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


DOCTOLIB_ORIGIN = "https://www.doctolib.de"
CLOUDINARY_BASE = "https://media.doctolib.com/image/upload/q_auto:eco,f_auto"


def normalize_doctolib_url(raw_url: str | None) -> str | None:
    if not raw_url:
        return None
    cleaned = raw_url.strip()
    if not cleaned:
        return None
    if cleaned.startswith("//"):
        return f"https:{cleaned}"
    if cleaned.startswith("/"):
        return f"{DOCTOLIB_ORIGIN}{cleaned}"
    if not cleaned.startswith(("http://", "https://")):
        return f"https://{cleaned}"
    return cleaned


class SeoSpecialtyLink(BaseModel):
    model_config = ConfigDict(extra="ignore")

    country: str = "DE"
    city: str
    speciality: str
    url: HttpUrl

    @field_validator("url", mode="before")
    @classmethod
    def _ensure_absolute_url(cls, value: Any) -> Any:
        if isinstance(value, str):
            return normalize_doctolib_url(value)
        return value

    @property
    def specialty_slug(self) -> str:
        parts = [p for p in urlparse(str(self.url)).path.split("/") if p]
        return parts[0] if parts else ""

    @property
    def city_slug(self) -> str:
        parts = [p for p in urlparse(str(self.url)).path.split("/") if p]
        return parts[1] if len(parts) > 1 else ""


class GeoLocation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    address: str | None = None
    city: str | None = ""
    country: str | None = "de"
    lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    lng: float | None = Field(default=None, ge=-180.0, le=180.0)
    zipcode: str | None = None

    @field_validator("address", "city", "zipcode", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class InsuranceSectorInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    split: bool | None = False
    type: str | None = None

    @property
    def normalized_sector(self) -> str:
        return (self.type or "").strip().lower()


class MatchedVisitMotive(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agenda_ids: list[int] = Field(default_factory=list, alias="agendaIds")
    allow_new_patients: bool | None = Field(default=True, alias="allowNewPatients")
    fee: float | None = None
    insurance_sector: InsuranceSectorInfo | None = Field(default=None, alias="insuranceSector")
    name: str | None = ""
    visit_motive_id: int | None = Field(default=None, alias="visitMotiveId")


class ProviderReferences(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    type: str | None = "public_profile"
    practice_id: int | None = Field(default=None, alias="practiceId")


class OnlineBookingInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    agenda_ids: list[int] = Field(default_factory=list, alias="agendaIds")
    telehealth: bool | None = False


class SpecialtyTag(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = ""
    slug: str | None = ""


class HealthcareProviderCard(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    composite_id: str = Field(alias="id")
    cloudinary_public_id: str | None = Field(default=None, alias="cloudinaryPublicId")
    languages: list[str] = Field(default_factory=list)
    profile_url: HttpUrl = Field(alias="link")
    location: GeoLocation = Field(default_factory=GeoLocation)
    name: str
    first_name: str | None = Field(default=None, alias="firstName")
    title: str | None = None
    gender: str | None = None
    entity_type: str = Field(default="INDIVIDUAL_PRACTITIONER", alias="type")
    references: ProviderReferences
    services: list[str] = Field(default_factory=list)
    exact_match: bool = Field(default=True, alias="exactMatch")
    matched_visit_motive: MatchedVisitMotive | None = Field(
        default=None, alias="matchedVisitMotive"
    )
    online_booking: OnlineBookingInfo | None = Field(default=None, alias="onlineBooking")
    regulation_sector: str | None = Field(default=None, alias="regulationSector")
    speciality: SpecialtyTag = Field(default_factory=SpecialtyTag)

    @field_validator("profile_url", mode="before")
    @classmethod
    def _normalize_link(cls, value: Any) -> Any:
        if isinstance(value, str):
            return normalize_doctolib_url(value)
        return value

    @field_validator("name", "first_name", "title", mode="before")
    @classmethod
    def _clean_names(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @property
    def profile_id(self) -> int:
        return self.references.id

    @property
    def practice_id(self) -> int:
        return self.references.practice_id or 0

    @property
    def profile_slug(self) -> str:
        path = urlparse(str(self.profile_url)).path.rstrip("/")
        return path.split("/")[-1] if path else str(self.profile_id)

    @property
    def full_display_name(self) -> str:
        parts = [p for p in (self.title, self.first_name, self.name) if p]
        return " ".join(parts).strip()

    @property
    def avatar_url(self) -> HttpUrl | None:
        if not self.cloudinary_public_id:
            return None
        return HttpUrl(f"{CLOUDINARY_BASE}/{self.cloudinary_public_id}.jpg")

    @property
    def is_telehealth(self) -> bool:
        return bool(self.online_booking and self.online_booking.telehealth)

    @property
    def is_bookable(self) -> bool:
        motive = self.matched_visit_motive
        return bool(
            motive
            and motive.visit_motive_id
            and (motive.agenda_ids or (self.online_booking and self.online_booking.agenda_ids))
        )

    @property
    def effective_agenda_ids(self) -> list[int]:
        if self.matched_visit_motive and self.matched_visit_motive.agenda_ids:
            return self.matched_visit_motive.agenda_ids
        if self.online_booking and self.online_booking.agenda_ids:
            return self.online_booking.agenda_ids
        return []


class HcpSearchResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    total: int = Field(default=0, ge=0)
    healthcare_providers: list[HealthcareProviderCard] = Field(
        default_factory=list, alias="healthcareProviders"
    )
