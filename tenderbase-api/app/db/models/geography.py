"""Geographic model: provinces → districts → municipalities.

South Africa has 9 provinces, metropolitan municipalities (category A),
district municipalities (category C) and local municipalities (category B).
No geographic rows are invented in code — data is imported from an
authoritative dataset via ``scripts/import_geography.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.enums import MunicipalityType


class Province(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A South African province."""

    __tablename__ = "provinces"
    __table_args__ = (UniqueConstraint("code", name="uq_provinces_code"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False, default="ZA")
    official_website: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    districts: Mapped[list[District]] = relationship(
        back_populates="province", cascade="all, delete-orphan", lazy="selectin"
    )
    municipalities: Mapped[list[Municipality]] = relationship(
        back_populates="province", lazy="noload"
    )


class District(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A district municipality grouping (category C) within a province."""

    __tablename__ = "districts"
    __table_args__ = (
        UniqueConstraint("code", name="uq_districts_code"),
        Index("ix_districts_province_id_name", "province_id", "name"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    slug: Mapped[str] = mapped_column(String(180), nullable=False, unique=True, index=True)
    province_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("provinces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    official_website: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    province: Mapped[Province] = relationship(back_populates="districts", lazy="joined")
    municipalities: Mapped[list[Municipality]] = relationship(
        back_populates="district", lazy="noload"
    )


class Municipality(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A metropolitan, district or local municipality."""

    __tablename__ = "municipalities"
    __table_args__ = (
        UniqueConstraint("code", name="uq_municipalities_code"),
        Index("ix_municipalities_province_id_type", "province_id", "type"),
        Index("ix_municipalities_name_lower", "name"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    slug: Mapped[str] = mapped_column(String(220), nullable=False, unique=True, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False, default=MunicipalityType.LOCAL)
    province_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("provinces.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    district_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("districts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    official_website: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Free-form alternative spellings used by the municipality-name normalizer.
    aliases: Mapped[dict | None] = mapped_column(nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Provenance of the geographic record (e.g. "demarcation-board-2016").
    data_source: Mapped[str | None] = mapped_column(String(120), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(nullable=True)

    province: Mapped[Province] = relationship(back_populates="municipalities", lazy="joined")
    district: Mapped[District | None] = relationship(back_populates="municipalities", lazy="joined")
    sources: Mapped[list[MunicipalitySource]] = relationship(  # noqa: F821
        back_populates="municipality", lazy="noload"
    )
