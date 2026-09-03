"""Load clearly-marked DEVELOPMENT FIXTURE data.

    ⚠️  TEST FIXTURE / DEVELOPMENT DATA — NOT REAL PROCUREMENT INFORMATION ⚠️

Everything created here is flagged ``is_test_fixture=True`` and is excluded from
API responses and statistics unless a client explicitly opts in with
``include_test_fixtures=true``. Fixture organisations use the reserved
``example.org``/``example.gov.za`` domains so they can never be mistaken for a
real municipality or a real tender.

Refuses to run when ``APP_ENV=production``.

Usage::

    python -m scripts.load_dev_fixtures
    python -m scripts.load_dev_fixtures --purge
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta

from sqlalchemy import delete, select

from app.config import get_settings
from app.db.models.geography import Municipality, Province
from app.db.models.opportunity import ProcurementOpportunity
from app.db.models.source import MunicipalitySource
from app.db.session import session_scope
from app.enums import (
    ConnectorType,
    DataQuality,
    MunicipalityType,
    OpportunityStatus,
    ProcurementScope,
    ProcurementType,
    SourceType,
)
from app.logging import configure_logging, get_logger
from app.utils.dates import utcnow
from app.utils.hashing import content_hash, fingerprint
from app.utils.text import normalize_reference_number, slugify

logger = get_logger("scripts.fixtures")

FIXTURE_PREFIX = "TEST FIXTURE"
FIXTURE_MUNICIPALITY_CODE = "ZZFIXTURE"
FIXTURE_SOURCE_SLUG = "test-fixture-source"

FIXTURE_TENDERS = [
    {
        "reference": "FIXTURE/2026/001",
        "title": f"{FIXTURE_PREFIX}: Supply and delivery of solar photovoltaic equipment",
        "type": ProcurementType.RFQ,
        "status": OpportunityStatus.OPEN,
        "closes_in_days": 21,
    },
    {
        "reference": "FIXTURE/2026/002",
        "title": f"{FIXTURE_PREFIX}: Construction of a community water reticulation network",
        "type": ProcurementType.TENDER,
        "status": OpportunityStatus.OPEN,
        "closes_in_days": 5,
    },
    {
        "reference": "FIXTURE/2026/003",
        "title": f"{FIXTURE_PREFIX}: Provision of ICT support services (closed example)",
        "type": ProcurementType.RFP,
        "status": OpportunityStatus.CLOSED,
        "closes_in_days": -14,
    },
]


async def purge() -> int:
    """Remove every fixture record."""
    async with session_scope() as session:
        result = await session.execute(
            delete(ProcurementOpportunity).where(ProcurementOpportunity.is_test_fixture.is_(True))
        )
        await session.execute(
            delete(MunicipalitySource).where(MunicipalitySource.slug == FIXTURE_SOURCE_SLUG)
        )
        await session.execute(
            delete(Municipality).where(Municipality.code == FIXTURE_MUNICIPALITY_CODE)
        )
        removed = result.rowcount or 0
    logger.warning("fixtures.purged", removed=removed)
    return removed


async def load() -> dict[str, int]:
    """Create the fixture municipality, source and opportunities."""
    settings = get_settings()
    if settings.is_production:
        raise SystemExit("Refusing to load development fixtures in production")

    now = utcnow()
    async with session_scope() as session:
        province = (await session.execute(select(Province).limit(1))).scalars().first()
        if province is None:
            raise SystemExit(
                "Import provinces first: python -m scripts.import_geography --provinces"
            )

        municipality = (
            (
                await session.execute(
                    select(Municipality).where(Municipality.code == FIXTURE_MUNICIPALITY_CODE)
                )
            )
            .scalars()
            .first()
        )
        if municipality is None:
            municipality = Municipality(
                name=f"{FIXTURE_PREFIX} Municipality (not a real municipality)",
                code=FIXTURE_MUNICIPALITY_CODE,
                slug=slugify(f"{FIXTURE_PREFIX}-municipality"),
                type=str(MunicipalityType.LOCAL),
                province_id=province.id,
                official_website="https://example.org",
                active=False,
                data_source="development-fixture",
            )
            session.add(municipality)
            await session.flush()

        source = (
            (
                await session.execute(
                    select(MunicipalitySource).where(MunicipalitySource.slug == FIXTURE_SOURCE_SLUG)
                )
            )
            .scalars()
            .first()
        )
        if source is None:
            source = MunicipalitySource(
                name=f"{FIXTURE_PREFIX} source (example.org)",
                slug=FIXTURE_SOURCE_SLUG,
                organization=f"{FIXTURE_PREFIX} Municipality",
                source_type=str(SourceType.MUNICIPAL_RFQ),
                base_url="https://example.org",
                procurement_scope=str(ProcurementScope.MUNICIPAL),
                municipality_id=municipality.id,
                province_id=province.id,
                connector_type=str(ConnectorType.HTML),
                connector_key="html.listing",
                config={"listing_paths": ["/tenders"], "item_selector": "tr"},
                active=False,
                notes="DEVELOPMENT FIXTURE — points at example.org and is never crawled.",
            )
            session.add(source)
            await session.flush()

        created = 0
        for entry in FIXTURE_TENDERS:
            existing = (
                (
                    await session.execute(
                        select(ProcurementOpportunity).where(
                            ProcurementOpportunity.reference_number == entry["reference"]
                        )
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None:
                continue
            closing = now + timedelta(days=entry["closes_in_days"])
            payload = {
                "reference_number": entry["reference"],
                "title": entry["title"],
                "procurement_type": str(entry["type"]),
                "status": str(entry["status"]),
                "closing_at": closing,
            }
            session.add(
                ProcurementOpportunity(
                    reference_number=entry["reference"],
                    reference_number_normalized=normalize_reference_number(entry["reference"]),
                    title=entry["title"],
                    description=(
                        "DEVELOPMENT DATA. This record was created by "
                        "scripts/load_dev_fixtures.py and does not describe a real "
                        "procurement opportunity."
                    ),
                    procurement_type=str(entry["type"]),
                    status=str(entry["status"]),
                    organization=f"{FIXTURE_PREFIX} Municipality",
                    municipality_id=municipality.id,
                    province_id=province.id,
                    source_id=source.id,
                    published_at=now - timedelta(days=3),
                    closing_at=closing,
                    source_timezone="Africa/Johannesburg",
                    source_url=(
                        f"https://example.org/tenders/{entry['reference'].replace('/', '-')}"
                    ),
                    content_hash=content_hash(payload),
                    fingerprint=fingerprint(payload),
                    data_quality=str(DataQuality.VALID),
                    confidence=1.0,
                    first_seen_at=now,
                    last_seen_at=now,
                    is_test_fixture=True,
                )
            )
            created += 1

    logger.warning(
        "fixtures.loaded",
        created=created,
        warning="TEST FIXTURE DATA — not real procurement information",
    )
    return {"created": created}


def main() -> None:
    parser = argparse.ArgumentParser(description="Load development fixtures")
    parser.add_argument("--purge", action="store_true", help="Delete all fixture records")
    args = parser.parse_args()
    configure_logging(stream=sys.stderr)
    asyncio.run(purge() if args.purge else load())


if __name__ == "__main__":
    main()
