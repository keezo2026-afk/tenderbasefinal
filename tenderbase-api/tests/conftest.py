"""Shared pytest fixtures.

The suite runs against SQLite (via ``aiosqlite``) so it is fast, hermetic and
requires no external services. PostgreSQL-specific behaviour (full-text search,
trigram dedup) degrades to portable SQL — the tests assert the *contract*, not
the dialect. A dedicated integration test exercises the Alembic migrations.

No test ever contacts a live website: connectors are driven by fixture
responses through an ``httpx.MockTransport``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("HTTP_ALLOW_PRIVATE_NETWORKS", "true")
os.environ.setdefault("HTTP_RESPECT_ROBOTS", "false")
os.environ.setdefault("HTTP_DEFAULT_RATE_LIMIT_PER_MINUTE", "6000")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("STATISTICS_CACHE_SECONDS", "0")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

import httpx  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
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


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture
async def engine() -> AsyncIterator:
    """A fresh in-memory database per test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


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
                    return httpx.Response(status, text=body, headers={"content-type": content_type})
            return httpx.Response(404, text="not found", headers={"content-type": "text/plain"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        return HTTPFetcher(client=client)

    return _build
