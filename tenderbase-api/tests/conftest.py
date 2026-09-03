"""Shared pytest fixtures.

Two database backends, one contract
-----------------------------------
By default the suite runs on **in-memory SQLite** (``aiosqlite``): fast, hermetic,
no services required. Set ``TEST_DATABASE_URL`` to a PostgreSQL URL and the
*same suite* runs against the production dialect::

    TEST_DATABASE_URL=postgresql+psycopg://tenderbase:tenderbase@127.0.0.1:5432/tenderbase_test \
        python -m pytest tests -q

In PostgreSQL mode each pytest session creates a throwaway schema
(``tenderbase_test_<random>``), builds it by running the **real Alembic chain**
(upgrade → downgrade → upgrade, which is itself a migration test), pins
``search_path`` for every connection, and drops the schema at the end. Tests
truncate between runs rather than re-running DDL, so isolation is cheap and every
committed write still starts from an empty database. Two developers, or two CI
jobs, can share one server without interference.

The tests assert the *contract*, not the dialect, except in the modules that
exist specifically to prove PostgreSQL behaviour (full-text ranking, trigram
deduplication) — those skip on SQLite with a reason.

No test ever contacts a live website: connectors are driven by fixture
responses through ``httpx.MockTransport``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

#: See the module docstring. Empty (default) means "use SQLite".
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()
IS_POSTGRES = TEST_DATABASE_URL.startswith("postgresql")

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL or "sqlite+aiosqlite:///:memory:")
# Authentication is exercised by dedicated tests; the rest of the suite must not
# depend on it. Rate limiting is likewise opted into explicitly.
os.environ.setdefault("API_KEY_ENFORCEMENT_ENABLED", "false")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("HTTP_ALLOW_PRIVATE_NETWORKS", "true")
os.environ.setdefault("HTTP_RESPECT_ROBOTS", "false")
os.environ.setdefault("HTTP_DEFAULT_RATE_LIMIT_PER_MINUTE", "6000")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("STATISTICS_CACHE_SECONDS", "0")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-not-a-real-secret")

import httpx  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import app.connectors  # noqa: F401,E402 - registers connectors
from app.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.models.geography import Municipality, Province  # noqa: E402
from app.db.models.opportunity import ProcurementOpportunity  # noqa: E402
from app.db.models.source import MunicipalitySource  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.enums import (  # noqa: E402
    ConnectorType,
    DataQuality,
    MunicipalityType,
    OpportunityStatus,
    ProcurementScope,
    ProcurementType,
    SourceType,
)
from app.ingestion.fetcher import HTTPFetcher  # noqa: E402
from app.main import create_app  # noqa: E402
from app.utils.dates import utcnow  # noqa: E402
from app.utils.hashing import content_hash, fingerprint  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture(scope="session")
def base_db_url() -> str:
    """The URL from the environment (the shared test database)."""
    return os.environ["DATABASE_URL"]


@pytest.fixture(scope="session")
def db_url(test_database: str | None) -> str:
    """The URL every test connects to: the throwaway database, else SQLite.

    Kept as a separate fixture from :func:`test_database` so tests that need a
    raw engine (concurrency probes) connect to exactly the same place the session
    fixture does.
    """
    return test_database or os.environ["DATABASE_URL"]


@pytest.fixture(scope="session")
async def test_database(base_db_url: str) -> AsyncIterator[str | None]:
    """Create an isolated throwaway **database** and build it from migrations.

    A separate database (rather than a schema inside a shared one) needs no
    ``search_path`` trickery: every connection, including Alembic's own, is
    naturally isolated, and dropping it leaves nothing behind. Two developers or
    two CI jobs can point at the same server without interfering.
    """
    if not IS_POSTGRES:
        yield None
        return

    database = f"tenderbase_test_{uuid4().hex[:12]}".lower()
    admin_url = base_db_url
    test_url = _swap_db_name(admin_url, database)

    admin = create_async_engine(admin_url, future=True, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
    await admin.dispose()

    # Extensions are per-database: create the one fuzzy deduplication and the
    # trigram indexes depend on before the migration runs.
    bootstrap = create_async_engine(test_url, future=True)
    async with bootstrap.begin() as connection:
        await connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    await bootstrap.dispose()

    from alembic import command
    from alembic.config import Config

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option("version_locations", str(REPO_ROOT / "migrations" / "versions"))
    # env.py prefers an explicit ``sqlalchemy.url`` over application settings,
    # which is what lets one migration chain serve an arbitrary database.
    config.set_main_option("sqlalchemy.url", test_url)
    # upgrade -> downgrade -> upgrade: the chain must be reversible *and*
    # idempotent, which is exactly what a production deploy needs.
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    try:
        yield test_url
    finally:
        cleanup = create_async_engine(admin_url, future=True, isolation_level="AUTOCOMMIT")
        async with cleanup.connect() as connection:
            await connection.exec_driver_sql(
                f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'
            )
        await cleanup.dispose()


def _swap_db_name(url: str, database: str) -> str:
    from sqlalchemy.engine import make_url

    return str(make_url(url).set(database=database))


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator:
    """A database scoped to one test.

    SQLite: an isolated in-memory database created from the models.
    PostgreSQL: a pool inside the migrated throwaway database, truncated per test.
    """
    if IS_POSTGRES:
        engine = create_async_engine(db_url, future=True, pool_size=5, max_overflow=5)
        try:
            async with engine.begin() as connection:
                await _truncate(connection)
            yield engine
        finally:
            await engine.dispose()
        return

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _truncate(connection) -> None:  # noqa: ANN001
    names = ", ".join(f'"{table.name}"' for table in reversed(Base.metadata.sorted_tables))
    await connection.exec_driver_sql(f"TRUNCATE {names} RESTART IDENTITY CASCADE")


@pytest.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session


@pytest.fixture
async def client(engine) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the app with the test database injected."""
    application = create_app()
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http_client:
        yield http_client
    application.dependency_overrides.clear()


# --- Domain fixtures ------------------------------------------------------


@pytest.fixture
async def province(session: AsyncSession) -> Province:
    province = Province(name="KwaZulu-Natal", code="KZN", slug="kwazulu-natal")
    session.add(province)
    await session.commit()
    return province


@pytest.fixture
async def municipality(session: AsyncSession, province: Province) -> Municipality:
    municipality = Municipality(
        name="Test Fixture Municipality",
        code="ZZTEST",
        slug="test-fixture-municipality",
        type=str(MunicipalityType.LOCAL),
        province_id=province.id,
        data_source="TEST FIXTURE",
    )
    session.add(municipality)
    await session.commit()
    return municipality


@pytest.fixture
async def source(session: AsyncSession, municipality: Municipality) -> MunicipalitySource:
    source = MunicipalitySource(
        name="TEST FIXTURE source",
        slug="test-fixture-source",
        organization="Test Fixture Municipality",
        source_type=str(SourceType.MUNICIPAL_RFQ),
        base_url="https://example.org",
        procurement_scope=str(ProcurementScope.MUNICIPAL),
        municipality_id=municipality.id,
        province_id=municipality.province_id,
        connector_type=str(ConnectorType.HTML),
        connector_key="html.listing",
        config={
            "listing_paths": ["/tenders"],
            "item_selector": "table.tenders tbody tr",
            "field_selectors": {
                "reference_number": "td:nth-child(1)",
                "title": "td:nth-child(2)",
                "published_at": "td:nth-child(3)",
                "closing_at": "td:nth-child(4)",
            },
            "link_selector": "td:nth-child(2) a",
        },
    )
    session.add(source)
    await session.commit()
    return source


@pytest.fixture
def make_opportunity(session: AsyncSession, source: MunicipalitySource, municipality: Municipality):
    """Factory creating clearly-marked fixture opportunities."""

    async def _make(
        *,
        title: str = "TEST FIXTURE: Supply of solar installation equipment",
        reference: str | None = None,
        status: OpportunityStatus = OpportunityStatus.OPEN,
        procurement_type: ProcurementType = ProcurementType.RFQ,
        closing_in_days: int = 14,
        published_days_ago: int = 3,
        is_fixture: bool = False,
    ) -> ProcurementOpportunity:
        now = utcnow()
        reference = reference or f"FIXTURE/{uuid4().hex[:8]}"
        payload = {
            "reference_number": reference,
            "title": title,
            "status": str(status),
            "closing_at": (now + timedelta(days=closing_in_days)).isoformat(),
        }
        opportunity = ProcurementOpportunity(
            reference_number=reference,
            reference_number_normalized=reference.upper(),
            title=title,
            description=f"{title} — development fixture description.",
            procurement_type=str(procurement_type),
            status=str(status),
            organization=source.organization,
            municipality_id=municipality.id,
            province_id=municipality.province_id,
            source_id=source.id,
            published_at=now - timedelta(days=published_days_ago),
            closing_at=now + timedelta(days=closing_in_days),
            source_timezone="Africa/Johannesburg",
            source_url=f"https://example.org/tenders/{reference.replace('/', '-')}",
            content_hash=content_hash(payload),
            fingerprint=fingerprint(payload, fields=("reference_number", "title", "closing_at")),
            data_quality=str(DataQuality.VALID),
            confidence=1.0,
            first_seen_at=now,
            last_seen_at=now,
            is_test_fixture=is_fixture,
        )
        session.add(opportunity)
        await session.commit()
        return opportunity

    return _make


# --- HTTP fixtures --------------------------------------------------------


def load_fixture(name: str) -> str:
    """Read a saved connector fixture response."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


@pytest.fixture
def fixture_loader() -> Callable[[str], str]:
    return load_fixture


@pytest.fixture
def mock_fetcher() -> Callable[[dict[str, tuple[int, str, str]]], HTTPFetcher]:
    """Build an :class:`HTTPFetcher` backed by canned responses.

    ``routes`` maps a URL (or a URL suffix) to ``(status, body, content_type)``.
    """

    def _build(routes: dict[str, tuple[int, str, str]]) -> HTTPFetcher:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            for pattern, (status, body, content_type) in routes.items():
                if url == pattern or url.endswith(pattern):
                    return httpx.Response(
                        status, text=body, headers={"content-type": content_type}
                    )
            return httpx.Response(404, text="not found", headers={"content-type": "text/plain"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        return HTTPFetcher(client=client)

    return _build


# --- helpers --------------------------------------------------------------


def pytest_configure(config):  # noqa: ANN001 - pytest hook
    config.addinivalue_line("markers", "postgres: requires the PostgreSQL test backend")
    config.addinivalue_line("markers", "redis: requires a reachable Redis server")


@pytest.fixture
def require_postgres():
    """Returns ``True`` on the PostgreSQL backend; skips the test otherwise.

    Used by tests that assert dialect-only behaviour (``pg_trgm``, full-text
    ranking). The complementary assertion (what SQLite must do instead) is made
    in the ``else`` branch of the same test, so both backends stay covered and a
    PostgreSQL-only assertion can never silently pass on SQLite.
    """

    def _check(*, skip: bool = True) -> bool:
        if not IS_POSTGRES and skip:
            pytest.skip("requires PostgreSQL (set TEST_DATABASE_URL)")
        return IS_POSTGRES

    return _check


#: Importable by test modules that need the flag directly.
requires_postgres = pytest.mark.skipif(not IS_POSTGRES, reason="requires PostgreSQL backend")
postgres_only = pytest.mark.postgres

__all__ = ["IS_POSTGRES", "text"]
