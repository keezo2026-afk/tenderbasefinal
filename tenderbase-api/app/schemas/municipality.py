"""Geographic response schemas."""

from __future__ import annotations

from uuid import UUID

from pydantic import Field

from app.enums import MunicipalityType
from app.schemas.common import TenderBaseModel


class ProvinceRef(TenderBaseModel):
    """Compact province reference embedded in other resources."""

    id: UUID
    name: str
    code: str


class ProvinceRead(ProvinceRef):
    slug: str
    official_website: str | None = None
    active: bool = True


class DistrictRef(TenderBaseModel):
    id: UUID
    name: str
    code: str


class DistrictRead(DistrictRef):
    slug: str
    province: ProvinceRef | None = None
    official_website: str | None = None
    active: bool = True


class MunicipalityRef(TenderBaseModel):
    """Compact municipality reference embedded in tender responses."""

    id: UUID
    name: str
    code: str
    type: MunicipalityType = MunicipalityType.LOCAL


class MunicipalityRead(MunicipalityRef):
    slug: str
    province: ProvinceRef | None = None
    district: DistrictRef | None = None
    official_website: str | None = None
    active: bool = True
    data_source: str | None = Field(
        default=None, description="Provenance of this geographic record"
    )


class MunicipalityFilter(TenderBaseModel):
    """Query filters for the municipality collection."""

    province: str | None = Field(default=None, description="Province name, code or slug")
    type: MunicipalityType | None = None
    q: str | None = Field(default=None, max_length=200, description="Name search")
    active: bool | None = None
