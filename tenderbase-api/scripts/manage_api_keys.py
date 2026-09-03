"""Mint, inspect, rotate and revoke API keys.

Credential minting is an *operator* action, so it lives here rather than behind an
HTTP endpoint that any authenticated caller could use to escalate themselves. (A
``POST /api/v1/api-keys`` route exists for provisioning-heavy deployments, but it
requires the ``admin`` scope and is disabled unless ``API_KEY_SELF_SERVICE_ENABLED``
is true.)

Usage::

    python -m scripts.manage_api_keys list
    python -m scripts.manage_api_keys stats
    python -m scripts.manage_api_keys create --name "Analytics export" --scopes readonly
    python -m scripts.manage_api_keys create --name ci \
        --scopes read:tenders read:statistics --expires-days 30
    python -m scripts.manage_api_keys check --key tb_live_ab12cd34...
    python -m scripts.manage_api_keys revoke --id 4a2b... --reason "laptop retired"
    python -m scripts.manage_api_keys rotate --id 4a2b...

The raw key is printed once, by ``create`` and ``rotate`` only. It is stored as a
keyed HMAC-SHA256 digest, so a lost key cannot be recovered — only rotated. If a
deployment ever sets ``API_KEY_PEPPER``, changing it invalidates every stored
digest, and ``list`` will keep working while every request starts failing with
401: that is the signal to re-issue, not to restore a backup.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.config import get_settings
from app.db.models.security import ApiKey
from app.db.session import session_scope
from app.enums import API_KEY_SCOPES
from app.errors import ValidationError
from app.logging import configure_logging, get_logger
from app.services.api_key_service import (
    ApiKeyService,
    AuthenticationError,
    IssuedKey,
    hash_api_key,
    parse_scopes,
)
from app.utils.dates import utcnow

logger = get_logger("scripts.manage_api_keys")


def expires_from(days: int | None) -> Any:
    """Turn ``--expires-days`` into a timestamp; ``None`` means "never expires"."""
    if days is None:
        return None
    if days < 1:
        raise SystemExit("--expires-days must be at least 1")
    return utcnow() + timedelta(days=days)


async def create_key(
    session: Any,
    *,
    name: str,
    scopes: list[str] | None,
    expires_days: int | None,
    created_by: str | None,
    notes: str | None,
) -> IssuedKey:
    service = ApiKeyService(session)
    return await service.create(
        name=name,
        scopes=parse_scopes(scopes) if scopes else None,
        expires_at=expires_from(expires_days),
        created_by=created_by,
        notes=notes,
    )


async def find_key(session: Any, identifier: str) -> ApiKey:
    """Look a key up by id, or by exact name when the id is not a UUID."""
    try:
        key_id = UUID(str(identifier))
    except ValueError:
        key_id = None
    if key_id is not None:
        found = (await session.execute(select(ApiKey).where(ApiKey.id == key_id))).scalars().first()
        if found is not None:
            return found
        raise SystemExit(f"No API key with id {identifier}")

    rows = (
        (await session.execute(select(ApiKey).where(ApiKey.name == identifier))).scalars().all()
    )
    if not rows:
        raise SystemExit(f"No API key named {identifier!r}")
    if len(rows) > 1:
        raise SystemExit(
            f"{len(rows)} keys are named {identifier!r}; pass a key id instead of a name"
        )
    return rows[0]


def describe(key: ApiKey) -> dict[str, Any]:
    """Metadata only — there is nothing here that reveals a credential."""
    return {
        "id": str(key.id),
        "name": key.name,
        "prefix": key.key_prefix,
        "status": key.status,
        "scopes": list(key.scopes or []),
        "created_at": key.created_at.isoformat() if key.created_at else None,
        "created_by": key.created_by,
        "expires_at": key.expires_at.isoformat() if key.expires_at else None,
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        "last_used_ip": key.last_used_ip,
        "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
        "revoked_reason": key.revoked_reason,
        "expired": bool(key.expires_at and key.expires_at <= utcnow()),
    }


async def lookup_raw_key(session: Any, raw_key: str) -> ApiKey | None:
    """Resolve a presented key to its row, the way the request path does."""
    digest = hash_api_key(raw_key, pepper=get_settings().key_pepper)
    return (
        (await session.execute(select(ApiKey).where(ApiKey.key_hash == digest))).scalars().first()
    )


def format_key(key: ApiKey) -> str:
    row = describe(key)
    state = row["status"] + (" (expired)" if row["expired"] else "")
    return (
        f"{row['id']}  {row['prefix']:<28} {state:<18} "
        f"{len(row['scopes'])} scope(s)  last used {row['last_used_at'] or 'never'}  "
        f"expires {row['expires_at'] or 'never'}"
        f"\n    {row['name']}"
    )


async def run(args: argparse.Namespace) -> int:
    async with session_scope() as session:
        service = ApiKeyService(session)

        if args.command == "list":
            keys = await service.list_keys(include_revoked=not args.active_only)
            if args.json:
                print(json.dumps([describe(key) for key in keys], indent=2))
                return 0
            if not keys:
                print("No API keys exist yet.")
                return 0
            for key in keys:
                print(format_key(key))
            return 0

        if args.command == "stats":
            stats = await service.stats()
            if args.json:
                print(json.dumps({"keys_by_status": stats}, indent=2))
                return 0
            for status, count in sorted(stats.items()):
                print(f"{status:<10} {count}")
            print(f"\nValid scopes: {', '.join(API_KEY_SCOPES)}")
            return 0

        if args.command == "create":
            issued = await create_key(
                session,
                name=args.name,
                scopes=args.scopes,
                expires_days=args.expires_days,
                created_by=args.created_by,
                notes=args.notes,
            )
            _print_new_key(issued)
            return 0

        if args.command == "check":
            if not args.key.strip():
                raise SystemExit(
                    "--key is empty. Pass the full value, e.g. --key \"tb_live_...\" "
                    "(quote it: a leading dash or shell history expansion eats key material)"
                )
            key = await lookup_raw_key(session, args.key)
            if key is None:
                print("That key is not recognised.", file=sys.stderr)
                return 2
            if not key.is_valid:
                print(f"Key found but not usable: status={key.status}, expires_at={key.expires_at}")
                return 2
            print(f"Key is valid. name={key.name} scopes={', '.join(key.scope_set)}")
            return 0

        if args.command in {"revoke", "rotate"}:
            key = await find_key(session, args.id)
            if args.command == "revoke":
                await service.revoke(str(key.id), reason=args.reason)
                print(f"Revoked {key.name} ({key.key_prefix}).")
                return 0
            issued = await service.create(
                name=key.name,
                scopes=list(key.scopes or []),
                expires_at=expires_from(args.expires_days),
                created_by=args.created_by,
                notes=f"Rotated from {key.key_prefix}",
            )
            await service.revoke(str(key.id), reason=args.reason or "Rotated")
            _print_new_key(issued)
            return 0

        raise SystemExit(f"Unknown command {args.command}")


def _print_new_key(issued: IssuedKey) -> None:
    print(
        "\nThis is the only time the key value is shown. It is stored only as a\n"
        "keyed digest, so there is no way to read it back later.\n"
    )
    print(f"  X-API-Key: {issued.raw_key}\n")
    print(f"  id:         {issued.key_id}")
    print(f"  name:       {issued.name}")
    print(f"  scopes:     {', '.join(issued.scopes)}")
    print(f"  expires_at: {issued.expires_at or 'never'}")
    if get_settings().enforce_api_keys:
        print("\n  This deployment enforces API keys: requests without one get 401.")
    else:
        print(
            "\n  Note: API_KEY_ENFORCEMENT_ENABLED is off in this environment, so the\n"
            "  key is accepted but not required. Set it in production."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    # ``--json`` belongs on the subcommands, not the root parser: a global flag
    # before the subcommand would make ``list --json`` an argument error.
    machine = argparse.ArgumentParser(add_help=False)
    machine.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable output (never includes a key value)",
    )

    listing = sub.add_parser(
        "list", parents=[machine], help="Show every key's metadata (never the value)"
    )
    listing.add_argument("--active-only", action="store_true", help="Hide revoked keys")

    sub.add_parser("stats", parents=[machine], help="Count keys by status")

    create = sub.add_parser("create", help="Mint a new key")
    create.add_argument("--name", required=True, help="Human label, e.g. \"City analytics export\"")
    create.add_argument(
        "--scopes",
        nargs="*",
        default=None,
        help=f"Scope names or a preset. Allowed: {', '.join(API_KEY_SCOPES)}",
    )
    create.add_argument("--expires-days", type=int, default=None, help="Omit for no expiry")
    create.add_argument("--created-by", default=None, help="Who asked for it (audit trail)")
    create.add_argument("--notes", default=None)

    check = sub.add_parser("check", help="Test a key value against the database")
    check.add_argument("--key", required=True)

    revoke = sub.add_parser("revoke", help="Revoke a key immediately")
    revoke.add_argument("--id", required=True, help="Key id, or its exact name")
    revoke.add_argument("--reason", default=None)

    rotate = sub.add_parser("rotate", help="Issue a replacement and revoke the old one")
    rotate.add_argument("--id", required=True)
    rotate.add_argument("--expires-days", type=int, default=None)
    rotate.add_argument("--created-by", default=None)
    rotate.add_argument("--reason", default=None)
    return parser


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    args = build_parser().parse_args(argv)
    try:
        raise SystemExit(asyncio.run(run(args)))
    except (AuthenticationError, ValidationError) as exc:
        # A bad name, an unknown scope or an empty key is a typo, not a crash:
        # print the reason and exit non-zero instead of dumping a traceback.
        detail = ", ".join(exc.details.get("allowed", [])) if exc.details else ""
        raise SystemExit(f"{exc}{f' (allowed: {detail})' if detail else ''}") from exc


if __name__ == "__main__":
    main()
