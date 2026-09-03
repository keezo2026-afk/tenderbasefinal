"""Verify a source against its real website, and record what was found.

This is the tool an operator uses before activating a source. It performs the
procedure described in ``docs/INGESTION.md`` — DNS, URL safety, connector
compatibility, listing discovery, one real fetch, parse, document and detail
discovery, robots policy, rate-limit behaviour — and prints every check with its
evidence.

Reading the output honestly matters::

    a 200 response is one check among twelve, not a verdict.

A source is only ever as good as the *evidence* collected here, and a passing run
means "reachable and parseable on <date>", not "authoritative", "complete" or
"permanently working". Verification never marks a source ACTIVE: activation is a
separate, deliberate decision (see ``--activate`` below for why that is still a
guard-railed action rather than a shortcut).

Usage::

    python -m scripts.verify_source --slug city-of-example-tenders
    python -m scripts.verify_source <source-id> --sample-items 5 --json
    python -m scripts.verify_source <source-id> --no-store          # dry run
    python -m scripts.verify_source --discover                     # every unverified source
    python -m scripts.verify_source <source-id> --activate --reason "checked by Nomsa"

Requires network access to the source. In an environment with no route to the
public internet every source reports FAILED for the network checks — that is the
correct answer, not a bug to work around: TenderBase records what it observed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from sqlalchemy import or_, select

from app.db.models.source import MunicipalitySource
from app.db.session import session_scope
from app.enums import SourceLifecycle
from app.ingestion.fetcher import HTTPFetcher
from app.logging import configure_logging, get_logger
from app.services.verification_service import SourceVerificationService

logger = get_logger("scripts.verify_source")


async def resolve_source(session: Any, identifier: str) -> MunicipalitySource:
    """Find a source by UUID or slug, failing loudly when it is neither."""
    from uuid import UUID

    try:
        source = (
            (
                await session.execute(
                    select(MunicipalitySource).where(MunicipalitySource.id == UUID(identifier))
                )
            )
            .scalars()
            .first()
        )
    except ValueError:
        source = None
    if source is None:
        source = (
            (
                await session.execute(
                    select(MunicipalitySource).where(MunicipalitySource.slug == identifier)
                )
            )
            .scalars()
            .first()
        )
    if source is None:
        raise SystemExit(f"No source matches id or slug {identifier!r}")
    return source


async def pending_sources(session: Any, *, limit: int) -> list[MunicipalitySource]:
    """Sources nobody has verified yet, oldest first."""
    rows = (
        (
            await session.execute(
                select(MunicipalitySource)
                .where(
                    or_(
                        MunicipalitySource.verification_status.is_(None),
                        MunicipalitySource.verification_status == "UNVERIFIED",
                    ),
                    MunicipalitySource.lifecycle_status.in_(
                        [
                            str(SourceLifecycle.DISCOVERED),
                            str(SourceLifecycle.PENDING_VERIFICATION),
                        ]
                    ),
                )
                .order_by(MunicipalitySource.created_at.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def render(outcome: Any) -> str:
    """Human-readable report, one line per check."""
    report = outcome.report
    lines = [
        f"Source   {outcome.slug}  ({outcome.source_id})",
        f"Target   {report.get('base_url')}",
        f"Outcome  {outcome.status} — {outcome.summary}",
        "",
        f"{'check':<12} {'status':<8} {'weight':<9} {'ms':>6}  detail",
    ]
    for check in report.get("checks", []):
        weight = "required" if check.get("required", True) else "optional"
        detail = (check.get("detail") or "").replace("\n", " ")
        lines.append(
            f"{check['name']:<12} {check['status']:<8} {weight:<9} "
            f"{check.get('duration_ms', 0):>6}  {detail[:110]}"
        )
    evidence = [
        (check["name"], check.get("evidence") or {})
        for check in report.get("checks", [])
        if check.get("evidence")
    ]
    if evidence:
        lines.append("")
        lines.append("Evidence")
        for name, payload in evidence:
            keys = ", ".join(f"{key}={value}" for key, value in list(payload.items())[:6])
            lines.append(f"  {name}: {keys[:150]}")
    lines.append("")
    lines.append(
        "A passing result means: reachable, parseable and document-discoverable on "
        f"{report.get('checked_at')}.\nIt does not mean the source is complete, "
        "authoritative, or still working tomorrow."
    )
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    exit_code = 0
    async with session_scope() as session:
        service = SourceVerificationService(session)
        if args.discover:
            targets = await pending_sources(session, limit=args.limit)
            if not targets:
                print("No unverified sources pending a check.")
                return 0
        else:
            if not args.source:
                raise SystemExit("Pass a source id/slug, or --discover")
            targets = [await resolve_source(session, args.source)]

        # One fetcher for the whole run: it owns the per-host rate limiter and the
        # robots cache, which is exactly what keeps a --discover loop polite.
        async with HTTPFetcher() as fetcher:
            for index, source in enumerate(targets, start=1):
                if len(targets) > 1:
                    print(f"\n=== {index}/{len(targets)} {source.slug} ===")
                outcome = await service.verify(
                    source.id,
                    fetcher=fetcher,
                    persist=not args.no_store,
                    sample_items=args.sample_items,
                )
                if args.json:
                    print(json.dumps(outcome.as_dict(), indent=2, default=str))
                else:
                    print(render(outcome))

                if args.activate and not args.no_store:
                    if outcome.status not in {"PASSED", "PASSED_WITH_WARNINGS"}:
                        print(
                            "  not activated: verification did not pass "
                            f"({outcome.status}). Fix the source and re-run."
                        )
                    else:
                        await service.set_lifecycle(
                            source.id, SourceLifecycle.ACTIVE, reason=args.reason
                        )
                        print(f"  lifecycle: {source.lifecycle_status}")
                elif args.activate:
                    raise SystemExit("--activate needs a stored result; drop --no-store")

                if outcome.status == "FAILED":
                    exit_code = 1
        if not args.no_store:
            await session.commit()
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", nargs="?", help="Source UUID or slug")
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Verify every source that is registered but not yet verified",
    )
    parser.add_argument("--limit", type=int, default=20, help="Cap for --discover")
    parser.add_argument(
        "--sample-items",
        type=int,
        default=3,
        help="Detail pages to fetch per source (bounded by the verifier's own cap)",
    )
    parser.add_argument(
        "--no-store",
        action="store_true",
        help="Report only; do not write verification columns or change lifecycle",
    )
    parser.add_argument(
        "--activate",
        action="store_true",
        help=(
            "Move the source to ACTIVE when the check passed. Deliberately not the "
            "default: a machine passing a checklist is not the same as a human "
            "deciding this source should be collected."
        ),
    )
    parser.add_argument("--reason", default=None, help="Audit note recorded with --activate")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    raise SystemExit(asyncio.run(run(build_parser().parse_args(argv))))


if __name__ == "__main__":
    main()
