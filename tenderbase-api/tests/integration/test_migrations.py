"""Alembic migration tests.

Production targets PostgreSQL, but the migration chain must remain
dialect-portable so it can be exercised in CI without a database server. This
test runs the whole chain against a throwaway SQLite file and asserts that the
resulting schema matches the SQLAlchemy models.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.db.base import Base

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Tables owned by the domain model (Alembic's own bookkeeping table excluded).
EXPECTED_TABLES = {
    "categories",
    "contacts",
    "districts",
    "document_text",
    "document_versions",
    "documents",
    "ingestion_errors",
    "ingestion_jobs",
    "municipalities",
    "municipality_sources",
    "opportunity_categories",
    "opportunity_events",
    "opportunity_versions",
    "procurement_opportunities",
    "provinces",
    "source_connectors",
    "source_runs",
}


@pytest.fixture
def alembic_config(tmp_path: Path) -> tuple[Config, str]:
    url = f"sqlite:///{tmp_path / 'migrations.sqlite3'}"
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config, url


def test_upgrade_head_creates_the_full_schema(alembic_config):
    config, url = alembic_config
    command.upgrade(config, "head")

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names()) - {"alembic_version"}
        assert EXPECTED_TABLES.issubset(tables)

        # Spot-check the spine of the schema.
        columns = {c["name"] for c in inspector.get_columns("procurement_opportunities")}
        assert {
            "id",
            "reference_number",
            "title",
            "content_hash",
            "fingerprint",
            "source_id",
            "municipality_id",
            "version",
            "is_test_fixture",
            "created_at",
            "updated_at",
        }.issubset(columns)

        foreign_keys = {
            fk["referred_table"] for fk in inspector.get_foreign_keys("procurement_opportunities")
        }
        assert {"municipality_sources", "municipalities", "provinces"}.issubset(foreign_keys)

        assert inspector.get_indexes("procurement_opportunities")
        unique = {
            tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("procurement_opportunities")
        }
        assert ("municipality_id", "reference_number") in unique
        assert ("source_id", "external_id") in unique
    finally:
        engine.dispose()


def test_migrations_match_the_models(alembic_config):
    """Every model table/column exists after the migrations run."""
    config, url = alembic_config
    command.upgrade(config, "head")

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        migrated = set(inspector.get_table_names()) - {"alembic_version"}
        modelled = set(Base.metadata.tables)
        assert modelled - migrated == set(), f"missing tables: {modelled - migrated}"

        for table_name in sorted(modelled):
            expected = {column.name for column in Base.metadata.tables[table_name].columns}
            actual = {column["name"] for column in inspector.get_columns(table_name)}
            assert expected - actual == set(), f"{table_name}: missing {expected - actual}"
    finally:
        engine.dispose()


def test_downgrade_to_base_is_clean(alembic_config):
    config, url = alembic_config
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine(url)
    try:
        remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
        assert remaining == set()
    finally:
        engine.dispose()
