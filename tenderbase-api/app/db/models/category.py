"""Categories and the opportunity ↔ category association."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:  # pragma: no cover - typing only, erased at runtime
    from app.db.models.opportunity import ProcurementOpportunity


class Category(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A procurement category (self-referencing taxonomy)."""

    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_categories_slug"),
        Index("ix_categories_parent_id_name", "parent_id", "name"),
    )

    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(180), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    #: Which taxonomy this term comes from, e.g. "tenderbase-core".
    taxonomy: Mapped[str] = mapped_column(String(60), nullable=False, default="tenderbase-core")
    #: Keywords used by the rule-based classifier (AI classification is optional).
    keywords: Mapped[dict | None] = mapped_column(nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    parent: Mapped[Category | None] = relationship(remote_side="Category.id", lazy="joined")


class OpportunityCategory(TimestampMixin, Base):
    """Association between an opportunity and a category, with confidence."""

    __tablename__ = "opportunity_categories"
    __table_args__ = (Index("ix_opportunity_categories_category_id", "category_id"),)

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("procurement_opportunities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    #: "RULE" | "AI" | "MANUAL" | "SOURCE"
    assigned_by: Mapped[str] = mapped_column(String(16), nullable=False, default="RULE")

    category: Mapped[Category] = relationship(lazy="joined")
    opportunity: Mapped[ProcurementOpportunity] = relationship(  # noqa: F821
        back_populates="categories"
    )
