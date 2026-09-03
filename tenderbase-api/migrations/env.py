"""Alembic environment.

The database URL comes from application settings (environment variables), so
migrations can never run against a hard-coded database. A caller may override
it explicitly — ``alembic -x sqlalchemy.url=...`` or by setting
``sqlalchemy.url`` on a programmatically-built :class:`~alembic.config.Config`
— which is how the migration tests target a throwaway database.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.db.base import Base  # noqa: F401 - imports every model for autogenerate

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()


def _database_url() -> str:
    """Explicit override first, application settings otherwise."""
    override = context.get_x_argument(as_dictionary=True).get("sqlalchemy.url")
    if override:
        return override
    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured
    return settings.sync_database_url


database_url = _database_url()
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata

#: PostgreSQL objects created by raw SQL in the search migration. They have no
#: SQLAlchemy model counterpart by design (expression indexes over SQL
#: expressions), so autogenerate must not propose dropping them.
DIALECT_SPECIFIC_INDEXES = frozenset(
    {
        "ix_opportunities_fts",
        "ix_document_text_fts",
        "ix_opportunities_title_trgm",
        "ix_municipalities_name_trgm",
        "ix_opportunities_open_closing",
    }
)


def include_object(object_, name, type_, reflected, compare_to) -> bool:  # noqa: ANN001, ANN201
    """Exclude dialect-only objects from autogenerate comparisons.

    Without this, ``alembic check``/``alembic revision --autogenerate`` run
    against PostgreSQL proposes ``DROP INDEX`` for every FTS/trigram index the
    search migration created — a silent, catastrophic foot-gun on the next
    generated revision.
    """
    if type_ == "index" and name in DIALECT_SPECIFIC_INDEXES:
        return False
    return True



def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = database_url
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
