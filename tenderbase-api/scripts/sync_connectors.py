"""Mirror the connector registry into the ``source_connectors`` table.

The API's ``GET /sources/connectors`` reads the live registry; this table is the
database-side copy that the source registry references and that operators inspect
with SQL. Keeping it current matters most for ``production_ready`` and
``status_note``: a connector that is not safe to run in production must not be
described as production-ready by a stale row, and a partially verified connector
(Playwright, the eTender OCDS interface) must carry its caveat here too.

The sync never *disables* a row that an operator turned off — it only reports it,
so a deliberate local opt-out survives a deploy.

Usage::

    python -m scripts.sync_connectors            # upsert
    python -m scripts.sync_connectors --dry-run  # show the diff, write nothing
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

import app.connectors  # noqa: F401 - populates the registry
from app.connectors.registry import list_connectors
from app.db.models.source import SourceConnector
from app.db.session import session_scope
from app.logging import configure_logging, get_logger

logger = get_logger("scripts.sync_connectors")


async def sync_connectors(*, dry_run: bool = False) -> dict[str, int]:
    stats = {"created": 0, "updated": 0, "unchanged": 0, "disabled_in_db": 0}
    async with session_scope() as session:
        for described in list_connectors():
            existing = (
                (
                    await session.execute(
                        select(SourceConnector).where(SourceConnector.key == described["key"])
                    )
                )
                .scalars()
                .first()
            )
            values = {
                "name": described["name"],
                "connector_type": described["connector_type"],
                "version": described["version"],
                "description": described["description"],
                "config_schema": described["config_schema"] or None,
                "requires_browser": described["requires_browser"],
                "production_ready": described["production_ready"],
                "status_note": described["status_note"],
                "enabled": True,
            }
            if existing is None:
                stats["created"] += 1
                if not dry_run:
                    session.add(SourceConnector(key=described["key"], **values))
                continue

            changed = {
                field: value
                for field, value in values.items()
                if field != "enabled" and getattr(existing, field) != value
            }
            # ``enabled`` is excluded above: a row an operator switched off stays
            # off, and is counted so the summary is not silently optimistic.
            if not existing.enabled:
                stats["disabled_in_db"] += 1
            if not changed:
                stats["unchanged"] += 1
                continue
            stats["updated"] += 1
            if not dry_run:
                for field, value in changed.items():
                    setattr(existing, field, value)
    logger.info("connectors.synced", dry_run=dry_run, **stats)
    return stats


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="Report the diff only")
    args = parser.parse_args()
    stats = asyncio.run(sync_connectors(dry_run=args.dry_run))
    if args.dry_run:
        summary = (
            f"dry run — {stats['created']} connector(s) would be created, "
            f"{stats['updated']} updated, {stats['unchanged']} already in sync"
        )
        if stats["disabled_in_db"]:
            summary += f", {stats['disabled_in_db']} disabled in the database"
        print(summary)


if __name__ == "__main__":
    main()
