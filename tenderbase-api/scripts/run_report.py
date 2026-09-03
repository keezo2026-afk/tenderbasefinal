"""Print the operator's daily picture: what ran, what failed, what needs a human.

One command instead of eight psql queries::

    python -m scripts.run_report
    python -m scripts.run_report --source city-of-example-tenders --history 10
    python -m scripts.run_report --failures 20
    python -m scripts.run_report --duplicates
    python -m scripts.run_report --json

Everything here is read-only, and every number is queried from the database —
including when it is zero. "No runs recorded" is a fact about the deployment, not
a placeholder to be filled in with something more encouraging.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.config import get_settings
from app.db.models.document import Document
from app.db.models.geography import Municipality
from app.db.models.opportunity import ProcurementOpportunity
from app.db.models.source import MunicipalitySource, SourceRun
from app.db.session import session_scope
from app.enums import DataQuality, HealthStatus, SourceLifecycle
from app.logging import configure_logging, get_logger
from app.services.operations_service import OperationsService
from app.utils.dates import utcnow

logger = get_logger("scripts.run_report")


def _age(value: Any) -> str:
    """Render a timestamp as a human age, so a stale source is obvious at a glance.

    Accepts a datetime or the ISO string the service layer emits, naive or aware:
    the two backends and the two layers disagree about all three.
    """
    if value is None:
        return "never"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    delta = utcnow() - moment
    if delta < timedelta(minutes=1):
        return f"{int(delta.total_seconds())}s ago"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)}m ago"
    if delta < timedelta(days=2):
        return f"{delta.seconds // 3600 + delta.days * 24}h ago"
    return f"{delta.days}d ago"


async def platform_totals(session: Any) -> dict[str, Any]:
    """Counts that describe how much data exists, and how old the freshest is."""
    sources = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(MunicipalitySource.active.is_(True)),
                func.count().filter(
                    MunicipalitySource.lifecycle_status == str(SourceLifecycle.ACTIVE)
                ),
                func.count().filter(
                    MunicipalitySource.health_status.in_(
                        [str(HealthStatus.FAILING), str(HealthStatus.OFFLINE)]
                    )
                ),
                func.max(MunicipalitySource.last_run_at),
            )
        )
    ).one()
    opportunities = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(ProcurementOpportunity.status == "OPEN"),
                func.count().filter(
                    ProcurementOpportunity.data_quality == str(DataQuality.NEEDS_REVIEW)
                ),
                func.max(ProcurementOpportunity.last_seen_at),
            )
        )
    ).one()
    documents = (await session.execute(select(func.count(), func.sum(Document.file_size)))).one()
    municipalities = (
        await session.execute(select(func.count()).select_from(Municipality))
    ).scalar_one()
    runs = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(SourceRun.status == "FAILED"),
                func.max(SourceRun.started_at),
            )
        )
    ).one()
    return {
        "sources": {
            "total": sources[0],
            "active": sources[1],
            "lifecycle_active": sources[2],
            "failing_or_offline": sources[3],
            "last_run": _age(sources[4]),
        },
        "opportunities": {
            "total": opportunities[0],
            "open": opportunities[1],
            "needs_review": opportunities[2],
            "last_seen": _age(opportunities[3]),
        },
        "documents": {"total": documents[0], "bytes": documents[1] or 0},
        "municipalities": municipalities,
        "runs": {"total": runs[0], "failed": runs[1], "last_started": _age(runs[2])},
    }


def format_totals(totals: dict[str, Any]) -> str:
    sources, opportunities, runs = totals["sources"], totals["opportunities"], totals["runs"]
    megabytes = round(totals["documents"]["bytes"] / (1024 * 1024), 1)
    return "\n".join(
        [
            f"Database    {get_settings().app_env} — last run {runs['last_started']}, "
            f"{runs['total']} runs ({runs['failed']} failed)",
            f"Sources     {sources['total']} registered, {sources['lifecycle_active']} activated, "
            f"{sources['failing_or_offline']} failing/offline, last crawl {sources['last_run']}",
            f"Data        {opportunities['total']} opportunities "
            f"({opportunities['open']} open, {opportunities['needs_review']} needing review), "
            f"last seen {opportunities['last_seen']}",
            f"Documents   {totals['documents']['total']} stored ({megabytes} MiB), "
            f"{totals['municipalities']} municipalities covered",
        ]
    )


def format_run(report: Any) -> str:
    verdict = f"  verdict={report.verdict}"
    if report.verdict_reason:
        verdict += f": {report.verdict_reason}"
    lines = [
        f"{report.source_slug}  {report.status}  {_age(report.completed_at or report.started_at)}"
        f"  found={report.items_found} created={report.items_created} "
        f"updated={report.items_updated} skipped={report.items_skipped} "
        f"failed={report.items_failed} docs={report.documents_found}"
        f" uncertain_dupes={report.uncertain_duplicates}",
        f"    run={report.run_id} duration={report.duration_ms}ms "
        f"http={report.http_status} errors={report.error_count}{verdict}",
    ]
    for error in report.errors[:3]:
        retry = "retryable" if error.retryable else "permanent"
        lines.append(f"    [{error.stage}/{error.code} {retry}] {error.message[:150]}")
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    async with session_scope() as session:
        service = OperationsService(session)

        if args.source:
            from scripts.verify_source import resolve_source

            source = await resolve_source(session, args.source)
            report = await service.latest_run_report(source.id)
            history = (
                await service.run_history(source.id, limit=args.history) if args.history else []
            )
            if args.json:
                payload = {
                    "latest": report.as_dict(),
                    "history": [item.as_dict() for item in history],
                }
                print(json.dumps(payload, indent=2, default=str))
                return 0
            print(format_run(report))
            if history:
                print(f"\nPrevious runs ({len(history)}):")
                for item in history[1:]:
                    print(format_run(item))
            return 0 if report.status != "FAILED" else 1

        if args.duplicates:
            candidates = await service.duplicate_review_queue(limit=args.limit)
            if args.json:
                print(json.dumps([candidate.__dict__ for candidate in candidates], indent=2))
                return 0
            if not candidates:
                print("No records are waiting for duplicate review.")
                return 0
            print(f"{len(candidates)} record(s) hold an uncertain duplicate match:\n")
            for candidate in candidates:
                match = candidate.matches[0] if candidate.matches else {}
                print(
                    f"  {candidate.opportunity_id}  {candidate.reference_number or '-'}  "
                    f"{candidate.title[:70]}"
                )
                print(
                    f"    candidate={match.get('existing_id')} layer={match.get('layer')} "
                    f"confidence={match.get('confidence')}"
                )
                if match.get("existing_title"):
                    print(f"    existing: {match['existing_title'][:70]}")
            print(
                "\nTenderBase never merges these automatically: the confidence is not "
                "high enough to justify rewriting history. Review each one — merge, "
                "split, or confirm both."
            )
            return 0

        totals = await platform_totals(session)
        unhealthy = await service.unhealthy_sources()
        failures = await service.failed_runs(limit=args.failures)
        pending = await service.duplicate_review_queue(limit=5)

        if args.json:
            print(
                json.dumps(
                    {
                        "generated_at": utcnow().isoformat(),
                        "totals": totals,
                        "unhealthy_sources": unhealthy,
                        "failed_runs": [report.as_dict() for report in failures],
                        "duplicate_review_count": len(pending),
                    },
                    indent=2,
                    default=str,
                )
            )
            return 0

        print(format_totals(totals))
        print(f"\nSources needing attention ({len(unhealthy)}):")
        if not unhealthy:
            print("  none — every active source is HEALTHY or UNKNOWN")
        for source in unhealthy[: args.limit]:
            print(
                f"  {source['slug']:<38} {source['health_status']:<9} "
                f"{source['consecutive_failures']} consecutive failure(s), "
                f"last run {_age(source['last_run_at'])}, "
                f"http={source['last_http_status']}"
            )
        print(f"\nRecent failed runs ({len(failures)}):")
        if not failures:
            print("  none recorded")
        for report in failures:
            print(format_run(report))
        if pending:
            print(
                f"\n{len(pending)} record(s) waiting for duplicate review "
                "(see --duplicates for the full list)"
            )
        else:
            print("\nNothing waiting for duplicate review")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", help="Report on one source (id or slug) instead of the fleet")
    parser.add_argument(
        "--history", type=int, default=0, help="Include N previous runs with --source"
    )
    parser.add_argument(
        "--failures", type=int, default=5, help="How many failed runs to list (0 hides them)"
    )
    parser.add_argument("--limit", type=int, default=10, help="Row cap per section")
    parser.add_argument("--duplicates", action="store_true", help="Show the duplicate review queue")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> None:
    configure_logging(stream=sys.stderr)
    raise SystemExit(asyncio.run(run(build_parser().parse_args(argv))))


if __name__ == "__main__":
    main()
