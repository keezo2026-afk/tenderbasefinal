"""Connector abstractions.

A **connector** knows how to turn a configured :class:`MunicipalitySource` into
raw procurement items. The pipeline (``app/ingestion``) owns normalization,
validation, deduplication, versioning and persistence — connectors stay small
and source-specific.

Lifecycle::

    discover(source)  -> [DiscoveryTarget]
    fetch(target)     -> FetchResult
    parse(result)     -> [RawItem]
    extract_documents(item) -> [DocumentCandidate]
    validate(item)    -> bool
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.enums import ConnectorType
from app.schemas.document import DocumentCandidate


@dataclass(slots=True)
class SourceContext:
    """Everything a connector needs about the source it is running against.

    A plain dataclass (rather than the ORM object) keeps connectors free of
    database concerns and trivially unit-testable with fixtures.
    """

    id: str
    name: str
    organization: str
    base_url: str
    connector_type: ConnectorType
    config: dict[str, Any] = field(default_factory=dict)
    source_type: str | None = None
    municipality_id: str | None = None
    province_id: str | None = None
    rate_limit_per_minute: int | None = None
    robots_policy: str = "RESPECT"
    source_timezone: str = "Africa/Johannesburg"

    def get(self, key: str, default: Any = None) -> Any:
        """Read a connector configuration key."""
        return self.config.get(key, default)

    @classmethod
    def from_model(cls, source: Any) -> SourceContext:
        """Build a context from a ``MunicipalitySource`` ORM row."""
        config = dict(source.config or {})
        return cls(
            id=str(source.id),
            name=source.name,
            organization=source.organization,
            base_url=source.base_url,
            connector_type=ConnectorType.parse(source.connector_type),
            config=config,
            source_type=str(source.source_type),
            municipality_id=str(source.municipality_id) if source.municipality_id else None,
            province_id=str(source.province_id) if source.province_id else None,
            rate_limit_per_minute=source.rate_limit_per_minute,
            robots_policy=source.robots_policy,
            source_timezone=config.get("timezone", "Africa/Johannesburg"),
        )


@dataclass(slots=True)
class DiscoveryTarget:
    """A concrete URL the connector intends to fetch."""

    url: str
    kind: str = "listing"  # listing | detail | document | feed
    method: str = "GET"
    params: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    depth: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FetchResult:
    """The outcome of fetching a :class:`DiscoveryTarget`."""

    target: DiscoveryTarget
    url: str
    status_code: int
    content: bytes
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    from_cache: bool = False
    encoding: str | None = None

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip().lower()

    @property
    def text(self) -> str:
        """Decoded body text (never raises on bad bytes)."""
        return self.content.decode(self.encoding or "utf-8", errors="replace")

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass(slots=True)
class RawItem:
    """A single procurement item as extracted by a connector.

    ``fields`` holds *source-shaped* values (still strings, still messy). The
    normalizer converts these into a :class:`NormalizedOpportunity`. The raw
    payload is preserved for audit.
    """

    source_url: str
    fields: dict[str, Any] = field(default_factory=dict)
    documents: list[DocumentCandidate] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    raw_html: str | None = None
    parser_metadata: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime | None = None

    def get(self, *keys: str, default: Any = None) -> Any:
        """Return the first present, non-empty value among ``keys``."""
        for key in keys:
            value = self.fields.get(key)
            if value not in (None, "", [], {}):
                return value
        return default


class ProcurementConnector(ABC):
    """Base class for all connectors.

    Subclasses implement at least :meth:`discover`, :meth:`fetch` and
    :meth:`parse`. Registration happens with the ``@register_connector``
    decorator in :mod:`app.connectors.registry`.
    """

    #: Unique registry key, e.g. ``"html.generic"``.
    key: str = "base"
    #: Human-readable name for the API's connector listing.
    name: str = "Base connector"
    connector_type: ConnectorType = ConnectorType.CUSTOM
    version: str = "0.1.0"
    description: str = ""
    requires_browser: bool = False
    #: Documentation of the accepted ``source.config`` keys.
    config_schema: dict[str, Any] = {}

    def __init__(self, fetcher: Any | None = None) -> None:
        #: Injected HTTP/browser fetcher — swapped for a fixture in tests.
        self.fetcher = fetcher

    # -- pipeline stages --------------------------------------------------

    @abstractmethod
    async def discover(self, source: SourceContext) -> Sequence[DiscoveryTarget]:
        """Return the listing/detail targets to fetch for this source."""

    @abstractmethod
    async def fetch(self, source: SourceContext, target: DiscoveryTarget) -> FetchResult:
        """Retrieve a single target."""

    @abstractmethod
    async def parse(self, source: SourceContext, response: FetchResult) -> Sequence[RawItem]:
        """Turn a fetched response into raw procurement items."""

    async def extract_documents(
        self, source: SourceContext, item: RawItem
    ) -> Sequence[DocumentCandidate]:
        """Return document candidates for an item (default: those already found)."""
        return item.documents

    async def validate(self, source: SourceContext, item: RawItem) -> bool:
        """Cheap connector-level sanity check before normalization."""
        title = item.get("title")
        return bool(title and str(title).strip())

    # -- convenience ------------------------------------------------------

    async def run(self, source: SourceContext) -> AsyncIterator[RawItem]:
        """Convenience driver: discover → fetch → parse, yielding items.

        Failures on a single target are propagated to the caller (the pipeline)
        which records them without aborting the remaining targets.
        """
        for target in await self.discover(source):
            response = await self.fetch(source, target)
            for item in await self.parse(source, response):
                if await self.validate(source, item):
                    item.documents = list(await self.extract_documents(source, item))
                    yield item

    def describe(self) -> dict[str, Any]:
        """Machine-readable connector description (exposed via the API)."""
        return {
            "key": self.key,
            "name": self.name,
            "connector_type": str(self.connector_type),
            "version": self.version,
            "description": self.description.strip(),
            "requires_browser": self.requires_browser,
            "config_schema": self.config_schema,
        }
