"""Import authoritative geographic reference data.

Provinces ship with the repository (ISO 3166-2:ZA codes — see
``data/geography/provinces.json``). **Districts and municipalities are not
bundled**: they must be imported from an authoritative dataset published by the
Municipal Demarcation Board / Statistics South Africa, because TenderBase never
fabricates municipality names, codes or websites.

Usage::

    python -m scripts.import_geography --provinces
    python -m scripts.import_geography --municipalities path/to/municipalities.csv \\
        --data-source "municipal-demarcation-board-2016"

Expected municipality CSV columns (header row required)::

    code,name,type,province_code,district_code,official_website

``type`` must be one of ``METROPOLITAN``, ``DISTRICT`` or ``LOCAL``. Rows whose
``type`` is ``DISTRICT`` also create/refresh the corresponding district record.
Unknown provinces are reported and skipped — never invented.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
from pathlib import Path

from sqlalchemy import select

from app.db.models.geography import District, Municipality, Province
from app.db.session import session_scope
from app.enums import MunicipalityType
from app.logging import configure_logging, get_logger
from app.utils.dates import utcnow
from app.utils.text import slugify
from app.utils.urls import is_http_url

logger = get_logger("scripts.import_geography")

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVINCES_FILE = REPO_ROOT / "data" / "geography" / "provinces.json"


async def import_provinces(path: Path = PROVINCES_FILE) -> int:
    """Upsert provinces from the bundled authoritative list."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    provenance = payload.get("provenance", {})
    created = 0
    async with session_scope() as session:
        for entry in payload["provinces"]:
            code = entry["code"].upper()
            existing = (
                (await session.execute(select(Province).where(Province.code == code)))
                .scalars()
                .first()
            )
            if existing:
                existing.name = entry["name"]
                continue
            session.add(
                Province(
                    name=entry["name"],
                    code=code,
                    slug=slugify(entry["name"]),
                    official_website=entry.get("official_website"),
                )
            )
            created += 1
    logger.info("geography.provinces_imported", created=created, source=provenance.get("source"))
    return created


async def import_municipalities(path: Path, *, data_source: str) -> dict[str, int]:
    """Import municipalities (and district records) from an authoritative CSV."""
    stats = {"created": 0, "updated": 0, "skipped": 0, "districts": 0}
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    if not rows:
        raise SystemExit(f"No rows found in {path}")

    required = {"code", "name", "type", "province_code"}
    missing = required - set(rows[0])
    if missing:
        raise SystemExit(f"CSV is missing required columns: {sorted(missing)}")

    async with session_scope() as session:
        provinces = {
            province.code.upper(): province
            for province in (await session.execute(select(Province))).scalars().all()
        }
        if not provinces:
            raise SystemExit(
                "Import provinces first: python -m scripts.import_geography --provinces"
            )

        districts: dict[str, District] = {
            district.code.upper(): district
            for district in (await session.execute(select(District))).scalars().all()
        }

        # Pass 1 — district municipalities, so local municipalities can link to them.
        for row in rows:
            if MunicipalityType.parse(row["type"]) is not MunicipalityType.DISTRICT:
                continue
            code = row["code"].strip().upper()
            province = provinces.get(row["province_code"].strip().upper())
            if province is None:
                stats["skipped"] += 1
                continue
            district = districts.get(code)
            if district is None:
                district = District(
                    name=row["name"].strip(),
                    code=code,
                    slug=slugify(f"{row['name']}-{code}"),
                    province_id=province.id,
                )
                session.add(district)
                districts[code] = district
                stats["districts"] += 1
            else:
                district.name = row["name"].strip()
        await session.flush()

        # Pass 2 — every municipality (including the district ones).
        existing_municipalities = {
            municipality.code.upper(): municipality
            for municipality in (await session.execute(select(Municipality))).scalars().all()
        }
        for row in rows:
            code = row["code"].strip().upper()
            province = provinces.get(row["province_code"].strip().upper())
            if province is None:
                logger.warning(
                    "geography.unknown_province", code=code, row=row.get("province_code")
                )
                stats["skipped"] += 1
                continue

            municipality_type = MunicipalityType.parse(row["type"])
            district_code = (row.get("district_code") or "").strip().upper()
            district = districts.get(district_code) if district_code else None
            website = (row.get("official_website") or "").strip() or None
            if website and not is_http_url(website):
                logger.warning("geography.invalid_website", code=code, website=website)
                website = None

            municipality = existing_municipalities.get(code)
            if municipality is None:
                session.add(
                    Municipality(
                        name=row["name"].strip(),
                        code=code,
                        slug=slugify(f"{row['name']}-{code}"),
                        type=str(municipality_type),
                        province_id=province.id,
                        district_id=district.id if district else None,
                        official_website=website,
                        data_source=data_source,
                        verified_at=utcnow(),
                    )
                )
                stats["created"] += 1
            else:
                municipality.name = row["name"].strip()
                municipality.type = str(municipality_type)
                municipality.province_id = province.id
                municipality.district_id = district.id if district else municipality.district_id
                municipality.official_website = website or municipality.official_website
                municipality.data_source = data_source
                municipality.verified_at = utcnow()
                stats["updated"] += 1

    logger.info("geography.municipalities_imported", **stats)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Import geographic reference data")
    parser.add_argument("--provinces", action="store_true", help="Import the bundled provinces")
    parser.add_argument("--municipalities", type=Path, help="Path to an authoritative CSV")
    parser.add_argument(
        "--data-source",
        default="operator-supplied",
        help="Provenance label stored on each municipality record",
    )
    args = parser.parse_args()
    configure_logging()

    if not args.provinces and not args.municipalities:
        parser.error("Specify --provinces and/or --municipalities")

    async def run() -> None:
        if args.provinces:
            await import_provinces()
        if args.municipalities:
            await import_municipalities(args.municipalities, data_source=args.data_source)

    asyncio.run(run())


if __name__ == "__main__":
    main()
