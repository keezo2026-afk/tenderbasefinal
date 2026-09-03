"""Date the right verification column.

Revision ID: d9e4b7f205c3
Revises: c7a3d5e81b40
Create Date: 2026-09-03

Fixes a CHECK constraint that made a passing verification impossible to record.

``ck_municipality_sources_passed_verification_is_dated`` was written as::

    verification_status <> 'PASSED' OR verified_at IS NOT NULL

Two things were wrong with that:

* ``verified_at`` is the **human** confirmation stamp; the automated procedure
  writes ``verification_at``. So a source whose verification *passed* — the only
  situation the constraint was meant to police — violated it, and
  ``SourceVerificationService._record`` died with an IntegrityError. The
  constraint rejected exactly the rows it existed to protect.
* ``PASSED_WITH_WARNINGS`` was not covered at all, so a source could be recorded
  as verified with no date beside it.

The replacement requires a date for *either* passing status and looks at the
column the service actually writes.

Batch mode is used for SQLite (``ALTER TABLE ... DROP CONSTRAINT`` does not
exist there); PostgreSQL gets plain ``ALTER`` statements. No data is touched, and
downgrading restores the previous definition verbatim for reversibility.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d9e4b7f205c3"
down_revision: str | None = "c7a3d5e81b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_SQL = "verification_status <> 'PASSED' OR verified_at IS NOT NULL"
_NEW_SQL = (
    "verification_status NOT IN ('PASSED', 'PASSED_WITH_WARNINGS') "
    "OR verification_at IS NOT NULL"
)


def _ck(name: str) -> str:
    """Same naming convention the models use, so autogenerate sees no drift."""
    return op.f(f"ck_municipality_sources_{name}")


def _replace_constraint(sql: str) -> None:
    with op.batch_alter_table("municipality_sources", recreate="auto") as batch_op:
        batch_op.drop_constraint(_ck("passed_verification_is_dated"), type_="check")
        batch_op.create_check_constraint(_ck("passed_verification_is_dated"), sql)


def upgrade() -> None:
    _replace_constraint(_NEW_SQL)


def downgrade() -> None:
    _replace_constraint(_OLD_SQL)
