"""Generic HTTP/JSON connector.

Reads a JSON (or JSON-ish) endpoint and maps its records onto raw items using
a declarative field map from the source configuration — no source-specific
code required.

Example ``source.config``::

    {
      "listing_paths": ["/api/tenders?page=1"],
      "records_path": "data.items",
      "field_map": {
        "title": "title",
        "reference_number": "referenceNumber",
        "published_at": "datePublished",
        "closing_at": "closingDate",
        "description": "summary",
        "detail_url": "links.self"
      },
      "document_path": "attachments",
      "document_url_key": "url"
    }
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from app.connectors.base import (
    DiscoveryTarget,
    FetchResult,
    ProcurementConnector,
    RawItem,
    SourceContext,
)
from app.connectors.registry import register_connector
from app.enums import ConnectorType, DocumentFormat
from app.errors import ParseError
from app.schemas.document import DocumentCandidate
from app.utils.dates import utcnow
from app.utils.urls import filename_from_url, normalize_url


def dig(payload: Any, path: str | None, default: Any = None) -> Any:
    """Resolve a dotted path inside nested dicts/lists (``a.b.0.c``)."""
    if not path:
        return default
    current = payload
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return default
        if current is None:
            return default
    return current


@register_connector(default_for_type=True)
class HTTPJSONConnector(ProcurementConnector):
    """Fetches JSON listings and maps fields declaratively."""

    key = "http.json"
    name = "Generic HTTP/JSON connector"
    connector_type = ConnectorType.HTTP
    description = """
    Fetches one or more JSON endpoints and maps records to procurement items
    using a declarative field map. Suitable for portals exposing an open data
    API or an internal JSON endpoint that is publicly accessible.
    """
    config_schema = {
        "listing_paths": "list[str] — paths or absolute URLs to fetch",
        "records_path": "str — dotted path to the array of records",
        "field_map": "dict[canonical_field -> dotted source path]",
        "document_path": "str — dotted path to a per-record document array",
        "document_url_key": "str — key holding the document URL",
        "detail_url_template": "str — optional template, e.g. '/tender/{id}'",
    }

    async def discover(self, source: SourceContext) -> Sequence[DiscoveryTarget]:
        paths = source.get("listing_paths") or ["/"]
        return [
            DiscoveryTarget(url=normalize_url(path, base=source.base_url), kind="listing")
            for path in paths
        ]

    async def fetch(self, source: SourceContext, target: DiscoveryTarget) -> FetchResult:
        if self.fetcher is None:  # pragma: no cover - guarded by the pipeline
            raise ParseError("No fetcher configured for connector")
        return await self.fetcher.fetch(
            target.url, source=source, target=target, headers={"Accept": "application/json"}
        )

    async def parse(self, source: SourceContext, response: FetchResult) -> Sequence[RawItem]:
        try:
            payload = json.loads(response.text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ParseError(
                f"Response is not valid JSON: {exc}", details={"url": response.url}
            ) from exc

        records_path = source.get("records_path")
        records = dig(payload, records_path, default=payload) if records_path else payload
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list):
            raise ParseError(
                "Could not locate a record array in the JSON payload",
                details={"url": response.url, "records_path": records_path},
            )

        field_map: dict[str, str] = source.get("field_map") or {}
        items: list[RawItem] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            fields = {name: dig(record, path) for name, path in field_map.items()}
            detail_url = fields.get("detail_url")
            if not detail_url and (template := source.get("detail_url_template")):
                try:
                    detail_url = template.format(**record)
                except (KeyError, IndexError, ValueError):
                    detail_url = None
            item_url = (
                normalize_url(str(detail_url), base=source.base_url) if detail_url else response.url
            )
            items.append(
                RawItem(
                    source_url=item_url,
                    fields={k: v for k, v in fields.items() if v is not None},
                    documents=list(self._documents(source, record)),
                    raw_payload=record,
                    parser_metadata={
                        "connector": self.key,
                        "connector_version": self.version,
                        "listing_url": response.url,
                    },
                    observed_at=utcnow(),
                )
            )
        return items

    def _documents(self, source: SourceContext, record: dict[str, Any]) -> list[DocumentCandidate]:
        path = source.get("document_path")
        if not path:
            return []
        raw_documents = dig(record, path, default=[]) or []
        if isinstance(raw_documents, dict):
            raw_documents = [raw_documents]
        url_key = source.get("document_url_key", "url")
        title_key = source.get("document_title_key", "title")

        candidates: list[DocumentCandidate] = []
        for entry in raw_documents:
            url = entry.get(url_key) if isinstance(entry, dict) else entry
            if not isinstance(url, str) or not url.strip():
                continue
            try:
                absolute = normalize_url(url, base=source.base_url)
            except Exception:  # noqa: BLE001 - a bad link must not fail the item
                continue
            filename = filename_from_url(absolute)
            candidates.append(
                DocumentCandidate(
                    source_url=absolute,
                    filename=filename,
                    title=entry.get(title_key) if isinstance(entry, dict) else None,
                    document_format=guess_format(filename),
                )
            )
        return candidates


def guess_format(filename: str | None) -> DocumentFormat:
    """Infer a document format from a filename extension."""
    if not filename or "." not in filename:
        return DocumentFormat.UNKNOWN
    return DocumentFormat.parse(filename.rsplit(".", 1)[-1])
