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
import json
import sys
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


def _emit(payload: dict, *, as_json: bool, human: str) -> None:
    """One line of JSON for a script, or one readable line for a terminal."""
    if as_json:
        print(json.dumps(payload, default=str))
    else:
        print(human)


async def run_one(source: MunicipalitySource, *, dry_run: bool, as_json: bool = False) -> None:
    async with HTTPFetcher() as fetcher:
        if dry_run:
            context = SourceContext.from_model(source)
            connector = build_connector(
                source.connector_key, source.connector_type, fetcher=fetcher
            )
            targets = await connector.discover(context)
            preview: list[dict] = []
            async for item in connector.run(context):
                preview.append(
                    {
                        "title": str(item.get("title"))[:100],
                        "reference_number": item.get("reference_number"),
                        "source_url": item.source_url,
                    }
                )
                if len(preview) >= 10:
                    break
            _emit(
                {
                    "mode": "dry-run",
                    "source_id": str(source.id),
                    "name": source.name,
                    "connector": connector.key,
                    "targets": [{"url": t.url, "kind": str(t.kind)} for t in targets],
                    "items_preview": preview,
                    "preview_truncated": len(preview) >= 10,
                },
                as_json=as_json,
                human=(
                    f"[dry-run] {source.name}: connector={connector.key}\n"
                    + "\n".join(f"  would fetch: {t.url} ({t.kind})" for t in targets)
                    + "\n"
                    + "\n".join(f"  item: {p['title']!r} -> {p['source_url']}" for p in preview)
                    + ("\n  … stopping after 10 items (dry run)" if len(preview) >= 10 else "")
                ),
            )
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
            _emit(
                {
                    "mode": "run",
                    "source_id": str(attached.id),
                    "name": attached.name,
                    "status": run.status,
                    "items_found": run.items_found,
                    "items_created": run.items_created,
                    "items_updated": run.items_updated,
                    "items_skipped": run.items_skipped,
                    "items_failed": run.items_failed,
                    "documents_found": run.documents_found,
                    "duration_ms": run.duration_ms,
                    "job_id": str(job.id),
                    "error": run.error_message,
                },
                as_json=as_json,
                human=(
                    f"{attached.name}: {run.status} "
                    f"found={run.items_found} created={run.items_created} "
                    f"updated={run.items_updated} skipped={run.items_skipped} "
                    f"failed={run.items_failed} documents={run.documents_found}"
                ),
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
            await run_one(source, dry_run=args.dry_run, as_json=args.json)
        except Exception as exc:  # noqa: BLE001 - one source must not stop the batch
            logger.error("ingestion.source_failed", source=source.name, error=str(exc))
            if args.json:
                # A batch that ends with a clean stdout and three sources missing
                # from it is indistinguishable from a batch where they succeeded,
                # so the failure is reported in the same stream as the results.
                print(
                    json.dumps(
                        {
                            "mode": "dry-run" if args.dry_run else "run",
                            "source_id": str(source.id),
                            "name": source.name,
                            "status": "FAILED",
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                        default=str,
                    )
                )


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
    parser.add_argument(
        "--json",
        action="store_true",
        help="One JSON object per source on stdout (for CI and dashboards)",
    )
    args = parser.parse_args()
    configure_logging(stream=sys.stderr)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
