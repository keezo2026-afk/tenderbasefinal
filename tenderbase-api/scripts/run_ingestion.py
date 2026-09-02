"""Run the ingestion pipeline for one source (or all due sources) from the CLI.

Useful for developing and verifying a connector without a worker or Redis.

Usage::

    python -m scripts.run_ingestion --source-id <uuid>
    python -m scripts.run_ingestion --slug example-municipality-rfq
    python -m scripts.run_ingestion --due --limit 5
    python -m scripts.run_ingestion --slug example --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from sqlalchemy import select

from app.connectors.base import SourceContext
from app.connectors.registry import build_connector
from app.db.models.ingestion import IngestionJob
from app.db.models.source import MunicipalitySource
from app.db.session import session_scope
from app.enums import JobStatus, JobTrigger
from app.ingestion.discovery import find_due_sources
from app.ingestion.fetcher import HTTPFetcher
from app.ingestion.pipeline import IngestionPipeline
from app.logging import configure_logging, get_logger
from app.utils.dates import utcnow

logger = get_logger("scripts.run_ingestion")


async def run_one(source: MunicipalitySource, *, dry_run: bool) -> None:
    async with HTTPFetcher() as fetcher:
        if dry_run:
            context = SourceContext.from_model(source)
            connector = build_connector(
                source.connector_key, source.connector_type, fetcher=fetcher
            )
            targets = await connector.discover(context)
            print(f"[dry-run] {source.name}: connector={connector.key}")
            for target in targets:
                print(f"  would fetch: {target.url} ({target.kind})")
            count = 0
            async for item in connector.run(context):
                count += 1
                print(f"  item: {str(item.get('title'))[:100]!r} -> {item.source_url}")
                if count >= 10:
                    print("  … stopping after 10 items (dry run)")
                    break
            return

        async with session_scope() as session:
            attached = await session.merge(source)
            job = IngestionJob(
                source_id=attached.id,
                job_type="SOURCE_INGEST",
                status=str(JobStatus.RUNNING),
                trigger=str(JobTrigger.MANUAL),
                started_at=utcnow(),
            )
            session.add(job)
            await session.flush()
            pipeline = IngestionPipeline(fetcher=fetcher)
            run = await pipeline.run_source(session, attached, job=job, commit=False)
            print(
                f"{attached.name}: {run.status} "
                f"found={run.items_found} created={run.items_created} "
                f"updated={run.items_updated} skipped={run.items_skipped} "
                f"failed={run.items_failed} documents={run.documents_found}"
            )


async def main_async(args: argparse.Namespace) -> None:
    async with session_scope() as session:
        if args.due:
            sources = list(await find_due_sources(session, limit=args.limit))
        elif args.source_id:
            sources = list(
                (
                    await session.execute(
                        select(MunicipalitySource).where(
                            MunicipalitySource.id == UUID(args.source_id)
                        )
                    )
                )
                .scalars()
                .all()
            )
        else:
            sources = list(
                (
                    await session.execute(
                        select(MunicipalitySource).where(MunicipalitySource.slug == args.slug)
                    )
                )
                .scalars()
                .all()
            )
        session.expunge_all()

    if not sources:
        raise SystemExit("No matching sources found")

    for source in sources:
        try:
            await run_one(source, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001 - one source must not stop the batch
            logger.error("ingestion.source_failed", source=source.name, error=str(exc))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ingestion pipeline")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source-id", help="Source UUID")
    group.add_argument("--slug", help="Source slug")
    group.add_argument("--due", action="store_true", help="Run every source that is due")
    parser.add_argument("--limit", type=int, default=10, help="Maximum sources when using --due")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse but persist nothing (connector development)",
    )
    args = parser.parse_args()
    configure_logging()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
