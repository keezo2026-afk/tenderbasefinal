"""PostgreSQL search support: pg_trgm, full-text and trigram indexes

Revision ID: b2f1c9d40a11
Revises: 27f45e7c21d7
Create Date: 2026-09-02

These objects are PostgreSQL-specific and are skipped on other dialects (the
test suite runs on SQLite, where the search service falls back to portable SQL
predicates). Creating the ``pg_trgm`` extension requires sufficient privileges;
if it fails the migration continues and fuzzy deduplication simply stays
disabled — the deduplicator degrades gracefully.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2f1c9d40a11"
down_revision: str | None = "27f45e7c21d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FTS_EXPRESSION = (
    "to_tsvector('english', "
    "coalesce(title, '') || ' ' || coalesce(reference_number, '') || ' ' "
    "|| coalesce(description, ''))"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Trigram support powers fuzzy (layer 4) deduplication and name matching.
    try:
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        trigram_available = True
    except Exception:  # noqa: BLE001 - insufficient privileges are not fatal
        trigram_available = False

    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_opportunities_fts "
            f"ON procurement_opportunities USING GIN ({FTS_EXPRESSION})"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_document_text_fts "
            "ON document_text USING GIN (to_tsvector('english', coalesce(content, '')))"
        )
    )

    if trigram_available:
        op.execute(
            sa.text(
                "CREATE INDEX IF NOT EXISTS ix_opportunities_title_trgm "
                "ON procurement_opportunities USING GIN (title gin_trgm_ops)"
            )
        )
        op.execute(
            sa.text(
                "CREATE INDEX IF NOT EXISTS ix_municipalities_name_trgm "
                "ON municipalities USING GIN (name gin_trgm_ops)"
            )
        )

    # Partial index for the most common query: currently open opportunities.
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_opportunities_open_closing "
            "ON procurement_opportunities (closing_at) WHERE status = 'OPEN'"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for index in (
        "ix_opportunities_open_closing",
        "ix_municipalities_name_trgm",
        "ix_opportunities_title_trgm",
        "ix_document_text_fts",
        "ix_opportunities_fts",
    ):
        op.execute(sa.text(f"DROP INDEX IF EXISTS {index}"))
