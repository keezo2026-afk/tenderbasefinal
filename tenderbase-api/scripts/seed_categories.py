"""Seed the built-in procurement category taxonomy.

This taxonomy is TenderBase's own reference data (not scraped, not fabricated
external data) and is safe to seed in any environment.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.db.models.category import Category
from app.db.session import session_scope
from app.logging import configure_logging, get_logger
from app.utils.text import slugify

logger = get_logger("scripts.seed_categories")

TAXONOMY = "tenderbase-core"

CATEGORIES: list[tuple[str, list[str]]] = [
    (
        "Construction & Civil Works",
        ["construction", "civil", "roads", "bridge", "building", "paving"],
    ),
    (
        "Electrical & Energy",
        ["electrical", "energy", "solar", "substation", "transformer", "generator", "photovoltaic"],
    ),
    (
        "Water & Sanitation",
        ["water", "sanitation", "sewer", "borehole", "reticulation", "pump station"],
    ),
    ("Waste Management", ["waste", "refuse", "landfill", "recycling", "cleaning services"]),
    (
        "Information Technology",
        ["ict", "software", "hardware", "network", "computer", "licences", "cyber"],
    ),
    (
        "Professional Services",
        [
            "consulting",
            "advisory",
            "audit",
            "legal",
            "engineering services",
            "professional services",
        ],
    ),
    ("Security Services", ["security", "guarding", "cctv", "access control", "alarm"]),
    ("Health & Medical", ["medical", "health", "clinic", "pharmaceutical", "ppe", "ambulance"]),
    ("Transport & Fleet", ["fleet", "vehicles", "transport", "tyres", "bus", "truck"]),
    ("Supply of Goods", ["supply and delivery", "goods", "materials", "stationery", "furniture"]),
    ("Maintenance & Repairs", ["maintenance", "repair", "servicing", "refurbishment"]),
    (
        "Training & Development",
        ["training", "skills", "learnership", "capacity building", "bursary"],
    ),
    (
        "Agriculture & Environment",
        ["agriculture", "environmental", "farming", "irrigation", "conservation"],
    ),
    ("Property & Facilities", ["lease", "property", "facilities", "office space", "rental"]),
    ("Disposal & Auction", ["disposal", "auction", "sale of assets", "scrap"]),
]


async def seed_categories() -> int:
    """Insert any missing categories. Existing rows are left untouched."""
    created = 0
    async with session_scope() as session:
        for name, keywords in CATEGORIES:
            slug = slugify(name)
            existing = (
                (await session.execute(select(Category).where(Category.slug == slug)))
                .scalars()
                .first()
            )
            if existing is not None:
                existing.keywords = {"terms": keywords}
                continue
            session.add(
                Category(
                    name=name,
                    slug=slug,
                    taxonomy=TAXONOMY,
                    keywords={"terms": keywords},
                )
            )
            created += 1
    logger.info("categories.seeded", created=created, total=len(CATEGORIES))
    return created


def main() -> None:
    configure_logging()
    asyncio.run(seed_categories())


if __name__ == "__main__":
    main()
