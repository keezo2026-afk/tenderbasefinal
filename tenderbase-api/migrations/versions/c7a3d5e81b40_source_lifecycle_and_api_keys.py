"""Source lifecycle, verification results and API key registry.

Revision ID: c7a3d5e81b40
Revises: b2f1c9d40a11
Create Date: 2026-09-03

Adds the Sprint 1 operational columns to the source registry (lifecycle state,
verification outcome, pause bookkeeping), connector readiness flags, and the
``api_keys`` table used by the authenticated read API.

Notes
-----
* Every new column is nullable or carries a ``server_default``, so no existing
  row is invented into a state it was not in.
* ``municipality_sources`` is altered through Alembic's **batch mode**: SQLite
  cannot ``ALTER TABLE ... ADD CONSTRAINT``, and this table has inbound foreign
  keys (``source_runs``, ``procurement_opportunities``, ``ingestion_errors``).
  Batch mode recreates the table on SQLite and emits plain ``ALTER`` statements
  on PostgreSQL (`recreate="auto"`), which keeps one migration for both.
* New sources default to ``DISCOVERED`` + ``UNVERIFIED``: the platform does not
  assume a registered source works.
* ``api_keys`` stores only an HMAC digest; there is deliberately no column
  capable of holding a plaintext key.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

from app.db.base_class import GUID

revision: str = "c7a3d5e81b40"
down_revision: str | None = "b2f1c9d40a11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=Text()).with_variant(sa.JSON(), "sqlite")


#: Matches the naming convention used by the models, so autogenerate sees no drift.
def _ck(name: str) -> str:
    return op.f(f"ck_municipality_sources_{name}")


def upgrade() -> None:
    with op.batch_alter_table("municipality_sources", recreate="auto") as batch_op:
        batch_op.add_column(
            sa.Column(
                "lifecycle_status",
                sa.String(length=24),
                server_default="DISCOVERED",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "verification_status",
                sa.String(length=24),
                server_default="UNVERIFIED",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("verification_result", _json_type(), nullable=True))
        batch_op.add_column(
            sa.Column("verification_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("verification_duration_ms", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("verification_http_status", sa.Integer(), nullable=True)
        )
        batch_op.add_column(sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("paused_reason", sa.String(length=500), nullable=True))
        batch_op.create_check_constraint(
            _ck("lifecycle_status_known"),
            "lifecycle_status IN ('DISCOVERED','PENDING_VERIFICATION','VERIFIED','ACTIVE',"
            "'DEGRADED','PAUSED','DISABLED')",
        )
        batch_op.create_check_constraint(
            _ck("verification_status_known"),
            "verification_status IN ('UNVERIFIED','PASSED','PASSED_WITH_WARNINGS','FAILED')",
        )
        batch_op.create_check_constraint(
            _ck("passed_verification_is_dated"),
            "verification_status <> 'PASSED' OR verified_at IS NOT NULL",
        )
        batch_op.create_index(
            "ix_municipality_sources_lifecycle_status", ["lifecycle_status"], unique=False
        )
        batch_op.create_index(
            "ix_municipality_sources_verification_status",
            ["verification_status"],
            unique=False,
        )

    # Sources already being scheduled keep working: an operator had (by having
    # set active=true) taken responsibility for them, so they map to ACTIVE
    # rather than being silently paused.
    op.execute(sa.text("UPDATE municipality_sources SET lifecycle_status = 'ACTIVE' WHERE active"))
    op.execute(
        sa.text("UPDATE municipality_sources SET lifecycle_status = 'PAUSED' WHERE NOT active")
    )

    op.add_column(
        "source_connectors",
        sa.Column("production_ready", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column("source_connectors", sa.Column("status_note", sa.Text(), nullable=True))

    op.create_table(
        "api_keys",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("key_prefix", sa.String(length=32), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="ACTIVE", nullable=False),
        sa.Column("scopes", _json_type(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_ip", sa.String(length=45), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=300), nullable=True),
        sa.Column("created_by", sa.String(length=160), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_api_keys_key_hash")),
        sa.CheckConstraint("status IN ('ACTIVE','REVOKED','EXPIRED')", name=op.f("ck_api_keys_status_known")),
        sa.CheckConstraint("length(key_prefix) >= 6", name=op.f("ck_api_keys_key_prefix_min_length")),
        sa.CheckConstraint(
            "revoked_at IS NOT NULL OR status <> 'REVOKED'",
            name=op.f("ck_api_keys_revoked_keys_are_stamped"),
        ),
    )
    op.create_index("ix_api_keys_status", "api_keys", ["status"], unique=False)
    op.create_index("ix_api_keys_key_prefix", "api_keys", ["key_prefix"], unique=False)
    op.create_index("ix_api_keys_expires_at", "api_keys", ["expires_at"], unique=False)
    op.create_index("ix_api_keys_created_at", "api_keys", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_api_keys_created_at", table_name="api_keys")
    op.drop_index("ix_api_keys_expires_at", table_name="api_keys")
    op.drop_index("ix_api_keys_key_prefix", table_name="api_keys")
    op.drop_index("ix_api_keys_status", table_name="api_keys")
    op.drop_table("api_keys")

    op.drop_column("source_connectors", "status_note")
    op.drop_column("source_connectors", "production_ready")

    with op.batch_alter_table("municipality_sources", recreate="auto") as batch_op:
        batch_op.drop_index("ix_municipality_sources_verification_status")
        batch_op.drop_index("ix_municipality_sources_lifecycle_status")
        for name in (
            "passed_verification_is_dated",
            "verification_status_known",
            "lifecycle_status_known",
        ):
            batch_op.drop_constraint(_ck(name), type_="check")
        for column in (
            "paused_reason",
            "paused_at",
            "verification_http_status",
            "verification_duration_ms",
            "verification_at",
            "verification_result",
            "verification_status",
            "lifecycle_status",
        ):
            batch_op.drop_column(column)
