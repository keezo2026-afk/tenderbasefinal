"""Import procurement source definitions from a JSON file.

Sources are **data**, not code: adding coverage means adding a verified entry
to a source file, not writing a new connector. This script never invents a
source — it imports exactly what an operator has verified.

Usage::

    python -m scripts.import_sources data/sources/example.json

File format (a list, or ``{"sources": [...]}``)::

    [
      {
        "name": "Example Municipality — RFQ notices",
        "organization": "Example Local Municipality",
        "source_type": "MUNICIPAL_RFQ",
        "connector_type": "WORDPRESS",
        "connector_key": "wordpress.rest",
        "base_url": "https://example.gov.za",
        "municipality_code": "XYZ",
        "config": {"post_type": "posts", "search": "tender"},
        "enabled": true,
        "crawl_frequency_minutes": 360,
        "notes": "Verified 2026-09-01; REST API publicly enabled."
      }
    ]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select

from app.connectors.registry import registered_keys
from app.db.models.geography import Municipality, Province
from app.db.models.source import MunicipalitySource
from app.db.session import session_scope
from app.enums import ProcurementScope
from app.logging import configure_logging, get_logger
from app.schemas.source import SourceDefinition
from app.utils.text import slugify
from app.utils.urls import validate_url

logger = get_logger("scripts.import_sources")


async def import_sources(path: Path, *, allow_unverified_urls: bool = False) -> dict[str, int]:
    """Upsert source definitions from ``path``."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload["sources"] if isinstance(payload, dict) else payload
    stats = {"created": 0, "updated": 0, "skipped": 0}
    known_keys = set(registered_keys())

    async with session_scope() as session:
        for raw in entries:
            try:
                definition = SourceDefinition.model_validate(raw)
            except ValidationError as exc:
                logger.error("sources.invalid_definition", error=str(exc), entry=raw)
                stats["skipped"] += 1
                continue

            check = validate_url(definition.base_url, check_dns=False)
            if not check.ok and not allow_unverified_urls:
                logger.error("sources.rejected_url", name=definition.name, reason=check.reason)
                stats["skipped"] += 1
                continue

            if definition.connector_key and definition.connector_key not in known_keys:
                logger.error(
                    "sources.unknown_connector",
                    name=definition.name,
                    connector_key=definition.connector_key,
                    known=sorted(known_keys),
                )
                stats["skipped"] += 1
                continue

            municipality_id = None
            province_id = None
            if definition.municipality_code:
                municipality = (
                    (
                        await session.execute(
                            select(Municipality).where(
                                Municipality.code == definition.municipality_code.upper()
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if municipality is None:
                    logger.error(
                        "sources.unknown_municipality",
                        name=definition.name,
                        code=definition.municipality_code,
                    )
                    stats["skipped"] += 1
                    continue
                municipality_id = municipality.id
                province_id = municipality.province_id
            if definition.province_code and province_id is None:
                province = (
                    (
                        await session.execute(
                            select(Province).where(
                                Province.code == definition.province_code.upper()
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                province_id = province.id if province else None

            slug = definition.slug or slugify(f"{definition.organization}-{definition.name}")
            existing = (
                (
                    await session.execute(
                        select(MunicipalitySource).where(MunicipalitySource.slug == slug)
                    )
                )
                .scalars()
                .first()
            )

            scope = definition.procurement_scope
            if scope is ProcurementScope.UNKNOWN and municipality_id:
                scope = ProcurementScope.MUNICIPAL

            values = {
                "name": definition.name,
                "organization": definition.organization,
                "source_type": str(definition.source_type),
                "base_url": check.url if check.ok else definition.base_url,
                "procurement_scope": str(scope),
                "municipality_id": municipality_id,
                "province_id": province_id,
                "connector_type": str(definition.connector_type),
                "connector_key": definition.connector_key,
                "config": definition.config or None,
                "active": definition.enabled,
                "priority": definition.priority,
                "crawl_frequency_minutes": definition.crawl_frequency_minutes,
                "rate_limit_per_minute": definition.rate_limit_per_minute,
                "robots_policy": definition.robots_policy,
                "notes": definition.notes,
                "verified_at": definition.verified_at,
            }

            if existing is None:
                session.add(MunicipalitySource(slug=slug, **values))
                stats["created"] += 1
            else:
                for field, value in values.items():
                    setattr(existing, field, value)
                stats["updated"] += 1

    logger.info("sources.imported", file=str(path), **stats)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Import procurement source definitions")
    parser.add_argument("path", type=Path, help="JSON file containing source definitions")
    parser.add_argument(
        "--allow-unverified-urls",
        action="store_true",
        help="Import even when a base_url fails validation (not recommended)",
    )
    args = parser.parse_args()
    configure_logging()
    asyncio.run(import_sources(args.path, allow_unverified_urls=args.allow_unverified_urls))


if __name__ == "__main__":
    main()
