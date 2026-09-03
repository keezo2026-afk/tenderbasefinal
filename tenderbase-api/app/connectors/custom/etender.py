"""National Treasury eTender (South Africa) connector — OCDS release parser.

Status: **interface + fixture-verified parser. NOT verified against the live
service from this build environment.**

Background
----------
The South African National Treasury / Office of the Chief Procurement Officer
publishes eTender data using the Open Contracting Data Standard (OCDS). The
transparency portal documents bulk downloads and a paginated "Release API"
(see https://data.etenders.gov.za/Home/LearnMore, and the OCP data registry
entry for "South Africa: National Treasury" which lists
``https://ocds-api.etenders.gov.za/swagger/index.html`` as the retrieval
endpoint). Documentation reviewed: 2026-09-02.

What this connector does
------------------------
It parses **OCDS release packages** — a published open standard, not a guessed
private format — and maps ``tender`` objects onto TenderBase raw items. The
endpoint path, query parameters and pagination style are supplied through
source configuration (``base_url`` + ``listing_paths``), so no speculative URL
is compiled into the code.

Known limitations
-----------------
* The live endpoint's exact paging parameters were not exercised here; operators
  must configure ``listing_paths`` from the current swagger document and set
  ``verified_at`` on the source once confirmed.
* Only the ``tender`` stage is mapped; ``awards`` and ``contracts`` are
  preserved in the raw payload for a later awards feature.
* Buyer names are mapped to ``organization``; municipality resolution happens
  in the normalizer via the municipality-name matcher, and stays ``NULL`` when
  no confident match exists.
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
from app.connectors.http import guess_format
from app.connectors.registry import register_connector
from app.enums import ConnectorType, ProcurementType
from app.errors import ParseError
from app.schemas.document import DocumentCandidate
from app.utils.dates import utcnow
from app.utils.text import clean_text
from app.utils.urls import filename_from_url, is_http_url, normalize_url

#: OCDS ``tender.mainProcurementCategory`` / ``procurementMethod`` hints mapped
#: onto TenderBase procurement types. Unknown values fall back to TENDER.
_OCDS_TYPE_HINTS: dict[str, ProcurementType] = {
    "open": ProcurementType.TENDER,
    "selective": ProcurementType.TENDER,
    "limited": ProcurementType.RFQ,
    "direct": ProcurementType.RFQ,
}

_OCDS_STATUS_MAP = {
    "planning": "UNKNOWN",
    "planned": "UNKNOWN",
    "active": "OPEN",
    "cancelled": "CANCELLED",
    "unsuccessful": "CLOSED",
    "complete": "AWARDED",
    "withdrawn": "CANCELLED",
}


@register_connector()
class ETenderOCDSConnector(ProcurementConnector):
    """Parses OCDS release packages from the National Treasury eTender API."""

    key = "custom.etender_ocds"
    name = "National Treasury eTender (OCDS)"
    connector_type = ConnectorType.CUSTOM
    #: The live retrieval endpoint contract has not been verified from a build
    #: environment with internet access to the service, so this connector is
    #: registered but never enabled by default. Configure ``base_url`` and
    #: ``listing_paths`` from the published swagger document, then run
    #: ``python -m scripts.verify_source <id>`` before setting it ACTIVE.
    production_ready = False
    status_note = (
        "UNVERIFIED against the live service: the OCDS release-package parser is "
        "fixture-tested, but the endpoint/pagination contract must be confirmed "
        "against the published API documentation before use."
    )
    version = "0.1.0"
    description = """
    Parses Open Contracting Data Standard (OCDS) release packages published by
    the South African National Treasury eTender / transparency portal. The
    endpoint and paging parameters are configuration-driven; the parser itself
    follows the OCDS 1.1 release schema. Not yet verified against live traffic.
    """
    config_schema = {
        "listing_paths": "list[str] — OCDS release endpoints (from the portal's swagger doc)",
        "releases_path": "str — dotted path to the releases array (default 'releases')",
        "next_link_path": "str — dotted path to the next-page link (default 'links.next')",
        "max_pages": "int — pagination safety limit (default 5)",
    }

    async def discover(self, source: SourceContext) -> Sequence[DiscoveryTarget]:
        paths = source.get("listing_paths")
        if not paths:
            raise ParseError(
                "custom.etender_ocds requires 'listing_paths' in the source configuration; "
                "no endpoint is hard-coded because the live API contract must be verified "
                "by an operator.",
                details={"source": source.name},
            )
        return [
            DiscoveryTarget(url=normalize_url(path, base=source.base_url), kind="listing")
            for path in paths
        ]

    async def fetch(self, source: SourceContext, target: DiscoveryTarget) -> FetchResult:
        if self.fetcher is None:  # pragma: no cover
            raise ParseError("No fetcher configured for connector")
        return await self.fetcher.fetch(
            target.url, source=source, target=target, headers={"Accept": "application/json"}
        )

    async def parse(self, source: SourceContext, response: FetchResult) -> Sequence[RawItem]:
        try:
            payload = json.loads(response.text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ParseError(
                "eTender response was not valid JSON", details={"url": response.url}
            ) from exc

        releases = payload.get("releases") if isinstance(payload, dict) else payload
        if isinstance(releases, dict):
            releases = [releases]
        if not isinstance(releases, list):
            raise ParseError(
                "OCDS payload did not contain a 'releases' array", details={"url": response.url}
            )

        items: list[RawItem] = []
        for release in releases:
            if not isinstance(release, dict):
                continue
            item = self._map_release(source, response, release)
            if item is not None:
                items.append(item)
        return items

    def _map_release(
        self, source: SourceContext, response: FetchResult, release: dict[str, Any]
    ) -> RawItem | None:
        tender = release.get("tender")
        if not isinstance(tender, dict):
            return None

        title = clean_text(tender.get("title")) or clean_text(release.get("title"))
        if not title:
            return None

        buyer = release.get("buyer") or {}
        parties = release.get("parties") or []
        buyer_name = clean_text(buyer.get("name")) or _first_party_name(parties, "buyer")

        tender_period = tender.get("tenderPeriod") or {}
        enquiry_period = tender.get("enquiryPeriod") or {}
        value = tender.get("value") or {}

        fields: dict[str, Any] = {
            "title": title,
            "description": clean_text(tender.get("description")),
            "reference_number": clean_text(tender.get("id")) or clean_text(release.get("ocid")),
            "external_id": clean_text(release.get("ocid")) or clean_text(release.get("id")),
            "organization": buyer_name,
            "published_at": release.get("date") or tender_period.get("startDate"),
            "closing_at": tender_period.get("endDate"),
            "procurement_type": self._procurement_type(tender),
            "status": _OCDS_STATUS_MAP.get(str(tender.get("status") or "").lower(), "UNKNOWN"),
            "estimated_value": value.get("amount"),
            "currency": value.get("currency"),
            "submission_method": _join(tender.get("submissionMethod")),
            "submission_url": _first_http(tender.get("submissionMethodDetails")),
            "enquiry_deadline": enquiry_period.get("endDate"),
        }

        # Briefing / site-meeting information is carried in OCDS milestones.
        for milestone in tender.get("milestones") or []:
            if not isinstance(milestone, dict):
                continue
            label = f"{milestone.get('title', '')} {milestone.get('description', '')}".lower()
            if any(word in label for word in ("briefing", "site meeting", "site inspection")):
                fields["briefing_date"] = milestone.get("dueDate") or milestone.get("dateMet")
                fields["briefing_location"] = clean_text(milestone.get("description"))
                fields["briefing_required"] = True
                break

        contact = tender.get("procuringEntity") or _party(parties, "procuringEntity") or {}
        contact_point = (contact.get("contactPoint") if isinstance(contact, dict) else None) or {}
        if contact_point:
            fields["contact_name"] = clean_text(contact_point.get("name"))
            fields["contact_email"] = clean_text(contact_point.get("email"))
            fields["contact_phone"] = clean_text(contact_point.get("telephone"))

        detail_url = _first_http(tender.get("documents"), key="url") or response.url

        return RawItem(
            source_url=detail_url,
            fields={k: v for k, v in fields.items() if v not in (None, "")},
            documents=self._documents(tender, base_url=response.url),
            raw_payload={
                "ocid": release.get("ocid"),
                "id": release.get("id"),
                "date": release.get("date"),
                "tag": release.get("tag"),
                "initiationType": release.get("initiationType"),
                "buyer": buyer,
                # Preserved for a future awards/contracts feature.
                "awards": release.get("awards"),
                "contracts": release.get("contracts"),
            },
            parser_metadata={
                "connector": self.key,
                "connector_version": self.version,
                "standard": "OCDS",
                "listing_url": response.url,
            },
            observed_at=utcnow(),
        )

    def _procurement_type(self, tender: dict[str, Any]) -> str:
        method = str(tender.get("procurementMethod") or "").lower()
        if mapped := _OCDS_TYPE_HINTS.get(method):
            return str(mapped)
        details = str(tender.get("procurementMethodDetails") or "").upper()
        for candidate in ("RFQ", "RFP", "RFB", "RFI", "EOI"):
            if candidate in details:
                return candidate
        return str(ProcurementType.TENDER)

    def _documents(self, tender: dict[str, Any], *, base_url: str) -> list[DocumentCandidate]:
        candidates: list[DocumentCandidate] = []
        seen: set[str] = set()
        for document in tender.get("documents") or []:
            if not isinstance(document, dict):
                continue
            url = document.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            try:
                absolute = normalize_url(url, base=base_url)
            except Exception:  # noqa: BLE001
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            filename = filename_from_url(absolute)
            candidates.append(
                DocumentCandidate(
                    source_url=absolute,
                    filename=filename,
                    title=clean_text(document.get("title")),
                    mime_type=document.get("format"),
                    document_format=guess_format(filename),
                )
            )
        return candidates


def _party(parties: list[Any], role: str) -> dict[str, Any] | None:
    for party in parties:
        if isinstance(party, dict) and role in (party.get("roles") or []):
            return party
    return None


def _first_party_name(parties: list[Any], role: str) -> str | None:
    party = _party(parties, role)
    return clean_text(party.get("name")) if party else None


def _join(value: Any) -> str | None:
    if isinstance(value, list):
        joined = ", ".join(str(v) for v in value if v)
        return joined or None
    return clean_text(value) if value else None


def _first_http(value: Any, key: str = "url") -> str | None:
    """Return the first http(s) URL found in a string, list or list of dicts."""
    if isinstance(value, str):
        return value if is_http_url(value) else None
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, dict) and is_http_url(entry.get(key, "")):
                return str(entry[key])
            if isinstance(entry, str) and is_http_url(entry):
                return entry
    return None
