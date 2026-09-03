"""Register the built-in connectors in the ``source_connectors`` table.

The table mirrors the in-process registry so operators can inspect available
implementations through the API and the database.

Usage::

    python -m scripts.sync_connectors
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

import app.connectors  # noqa: F401 - populates the registry
from app.connectors.registry import list_connectors
from app.db.models.source import SourceConnector
from app.db.session import session_scope
from app.logging import configure_logging, get_logger

logger = get_logger("scripts.sync_connectors")


async def sync_connectors() -> dict[str, int]:
    stats = {"created": 0, "updated": 0}
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
                "enabled": True,
            }
            if existing is None:
                session.add(SourceConnector(key=described["key"], **values))
                stats["created"] += 1
            else:
                for field, value in values.items():
                    setattr(existing, field, value)
                stats["updated"] += 1
    logger.info("connectors.synced", **stats)
    return stats


def main() -> None:
    configure_logging()
    asyncio.run(sync_connectors())


if __name__ == "__main__":
    main()
