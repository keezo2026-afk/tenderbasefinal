"""Source claiming: an eligibility horizon and a lease.

Revision ID: e6b07c9d41f2
Revises: d9e4b7f205c3
Create Date: 2026-09-03

Crawling used to be scheduled by reading `last_run_at`, deciding in Python that the
crawl interval had elapsed, and enqueueing. Two scheduler replicas — or one replica
whose tick overlapped a slow queue — therefore saw the same source as due at the same
moment and both enqueued it, because nothing in the database said "this one is
spoken for". `next_run_at` moves that decision into the row: claiming a source pushes
its own eligibility forward inside the same transaction that creates the job, so the
second claimer's `WHERE next_run_at IS NULL OR next_run_at <= now()` no longer matches.

`claim_expires_at` and `claim_job_id` make the claim *held* rather than *assumed*: a
worker that dies mid-run leaves a lease that expires, and reconciliation (or the next
tick, once the lease has passed) can take the source again. That pair is why the claim
cannot become a permanent lock, which is the failure mode transactional claiming usually
gets wrong.

Batch mode is used throughout so SQLite — which cannot `ALTER TABLE ADD CONSTRAINT` —
gets the same CHECK, and the downgrade drops exactly what was added.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.base_class import GUID

revision: str = "e6b07c9d41f2"
down_revision: str | None = "d9e4b7f205c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ck(name: str) -> str:
    """Same naming convention the models use, so autogenerate sees no drift."""
    return op.f(f"ck_municipality_sources_{name}")


def upgrade() -> None:
    with op.batch_alter_table("municipality_sources", recreate="auto") as batch_op:
        batch_op.add_column(sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("claim_job_id", GUID(), nullable=True))
        batch_op.create_index(
            "ix_municipality_sources_claim_due",
            ["active", "next_run_at"],
            unique=False,
        )
        batch_op.create_check_constraint(
            "claim_has_lease",
            "claim_job_id IS NULL OR claim_expires_at IS NOT NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("municipality_sources", recreate="auto") as batch_op:
        batch_op.drop_constraint(_ck("claim_has_lease"), type_="check")
        batch_op.drop_index("ix_municipality_sources_claim_due")
        batch_op.drop_column("claim_job_id")
        batch_op.drop_column("claim_expires_at")
        batch_op.drop_column("next_run_at")
