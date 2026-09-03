"""Normalization engine.

Converts messy, source-shaped :class:`RawItem` values into the canonical
:class:`NormalizedOpportunity`.

Principles
----------
* Never invent a value. If a field cannot be extracted with confidence the
  result is ``None``.
* Preserve the source: raw date strings, the raw payload and parser metadata
  travel with the record.
* Normalize to UTC internally while recording the source timezone.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from app.connectors.base import RawItem, SourceContext
from app.enums import OpportunityStatus, ProcurementType
from app.logging import get_logger
from app.schemas.tender import NormalizedOpportunity
from app.utils.dates import parse_closing_datetime, parse_datetime, utcnow
from app.utils.hashing import content_hash, fingerprint
from app.utils.text import (
    clean_text,
    extract_emails,
    extract_phones,
    normalize_reference_number,
    parse_money,
)
from app.utils.urls import is_http_url, normalize_url

logger = get_logger("tenderbase.normalizer")

MAX_DESCRIPTION_LENGTH = 20_000

#: Keyword hints used when a source does not state the procurement type.
TYPE_KEYWORDS: tuple[tuple[ProcurementType, tuple[str, ...]], ...] = (
    (ProcurementType.RFQ, ("rfq", "request for quotation", "quotation", "quote")),
    (ProcurementType.RFP, ("rfp", "request for proposal", "proposal")),
    (ProcurementType.RFB, ("rfb", "request for bid")),
    (ProcurementType.RFI, ("rfi", "request for information")),
    (
        ProcurementType.EXPRESSION_OF_INTEREST,
        ("expression of interest", "eoi", "call for expression"),
    ),
    (ProcurementType.AUCTION, ("auction", "disposal by auction")),
    (ProcurementType.TENDER, ("tender", "bid", "invitation to bid")),
)

STATUS_KEYWORDS: tuple[tuple[OpportunityStatus, tuple[str, ...]], ...] = (
    (OpportunityStatus.CANCELLED, ("cancelled", "canceled", "withdrawn")),
    (OpportunityStatus.AWARDED, ("awarded", "award notice", "successful bidder")),
    (OpportunityStatus.EXTENDED, ("extended", "extension of closing")),
    (OpportunityStatus.SUSPENDED, ("suspended", "on hold")),
    (OpportunityStatus.CLOSED, ("closed", "expired")),
    (OpportunityStatus.OPEN, ("open", "advertised", "current", "active", "invitation")),
)

BRIEFING_COMPULSORY_KEYWORDS = ("compulsory", "mandatory")
BRIEFING_KEYWORDS = ("briefing", "site meeting", "site inspection", "clarification meeting")


class Normalizer:
    """Turns raw connector items into canonical opportunities."""

    def __init__(self, *, default_timezone: str = "Africa/Johannesburg") -> None:
        self.default_timezone = default_timezone

    def normalize(
        self,
        item: RawItem,
        source: SourceContext,
        *,
        municipality_id: UUID | None = None,
        province_id: UUID | None = None,
    ) -> NormalizedOpportunity:
        """Normalize one raw item. Raises ``ValueError`` only for a missing title."""
        timezone = source.source_timezone or self.default_timezone

        title = clean_text(item.get("title", "name", "subject"), max_length=2000)
        if not title:
            raise ValueError("Cannot normalize an item without a title")

        raw_reference = clean_text(
            item.get("reference_number", "reference", "tender_number", "bid_number")
        )
        description = clean_text(
            item.get("description", "summary", "details", "body"),
            max_length=MAX_DESCRIPTION_LENGTH,
        )

        published = parse_datetime(
            item.get("published_at", "publication_date", "advertised_date", "date"),
            source_timezone=timezone,
        )
        closing = parse_closing_datetime(
            item.get("closing_at", "closing_date", "close_date", "deadline"),
            source_timezone=timezone,
        )
        briefing = parse_datetime(
            item.get("briefing_date", "briefing", "site_meeting_date"), source_timezone=timezone
        )

        raw_dates: dict[str, Any] = {}
        for label, parsed in (
            ("published_at", published),
            ("closing_at", closing),
            ("briefing_date", briefing),
        ):
            if parsed.raw:
                raw_dates[label] = {
                    "raw": parsed.raw,
                    "timezone": parsed.source_timezone,
                    "has_time": parsed.has_time,
                }

        currency, amount = self._value(item)
        procurement_type = self._procurement_type(item, title, description)
        status = self._status(item, title, closing_at=closing.value)

        source_url = self._url(item.source_url, base=source.base_url) or source.base_url
        canonical_url = self._url(item.get("canonical_url"), base=source.base_url)

        briefing_location = clean_text(item.get("briefing_location", "briefing_venue", "venue"))
        briefing_required = self._briefing_required(item, briefing.value, briefing_location)

        normalized = NormalizedOpportunity(
            external_id=clean_text(item.get("external_id", "id")),
            reference_number=raw_reference,
            reference_number_normalized=normalize_reference_number(raw_reference),
            title=title,
            description=description,
            procurement_type=procurement_type,
            status=status,
            organization=clean_text(item.get("organization", "department", "buyer"))
            or source.organization,
            municipality_id=municipality_id,
            province_id=province_id,
            source_id=UUID(source.id),
            published_at=published.value,
            closing_at=closing.value,
            source_timezone=timezone,
            raw_dates=raw_dates,
            estimated_value=amount,
            currency=currency,
            submission_method=clean_text(item.get("submission_method", "submission")),
            submission_url=self._url(item.get("submission_url"), base=source.base_url),
            submission_address=clean_text(item.get("submission_address", "delivery_address")),
            briefing_required=briefing_required,
            briefing_compulsory=self._briefing_compulsory(item),
            briefing_date=briefing.value,
            briefing_location=briefing_location,
            contact=self._contact(item),
            source_url=source_url,
            canonical_url=canonical_url,
            raw_payload=self._raw_payload(item),
            parser_metadata={
                **item.parser_metadata,
                "normalized_at": utcnow().isoformat(),
                "date_confidence": {
                    "published_at": published.confidence,
                    "closing_at": closing.confidence,
                },
            },
            documents=list(item.documents),
            confidence=self._confidence(published.confidence, closing.confidence, raw_reference),
        )

        payload = normalized.hashable_payload()
        normalized.content_hash = content_hash(payload)
        normalized.fingerprint = fingerprint(normalized.fingerprint_payload())
        return normalized

    # -- field helpers ----------------------------------------------------

    def _url(self, value: Any, *, base: str | None = None) -> str | None:
        if not value or not isinstance(value, str):
            return None
        try:
            candidate = normalize_url(value, base=base)
        except Exception:  # noqa: BLE001 - a malformed URL is simply absent
            return None
        return candidate if is_http_url(candidate) else None

    def _value(self, item: RawItem) -> tuple[str | None, Decimal | None]:
        raw = item.get("estimated_value", "value", "budget", "amount")
        if raw is None:
            return None, None
        if isinstance(raw, (int, float, Decimal)):
            try:
                amount = Decimal(str(raw))
            except InvalidOperation:
                return None, None
            currency = clean_text(item.get("currency")) or None
            return (currency.upper()[:3] if currency else None), (amount if amount >= 0 else None)
        currency, amount_text = parse_money(str(raw))
        if amount_text is None:
            return None, None
        try:
            amount = Decimal(amount_text)
        except InvalidOperation:
            return None, None
        explicit_currency = clean_text(item.get("currency"))
        return (explicit_currency.upper()[:3] if explicit_currency else currency), amount

    def _procurement_type(
        self, item: RawItem, title: str, description: str | None
    ) -> ProcurementType:
        explicit = item.get("procurement_type", "type", "category_type")
        if explicit:
            parsed = ProcurementType.parse(explicit)
            if parsed is not ProcurementType.OTHER:
                return parsed
            haystack_extra = f" {explicit}"
        else:
            haystack_extra = ""
        haystack = f"{title} {description or ''}{haystack_extra}".lower()
        for candidate, keywords in TYPE_KEYWORDS:
            if any(keyword in haystack for keyword in keywords):
                return candidate
        return ProcurementType.OTHER

    def _status(self, item: RawItem, title: str, *, closing_at: Any) -> OpportunityStatus:
        explicit = item.get("status", "state")
        if explicit:
            parsed = OpportunityStatus.parse(explicit)
            if parsed is not OpportunityStatus.UNKNOWN:
                return parsed
            lowered = str(explicit).lower()
            for candidate, keywords in STATUS_KEYWORDS:
                if any(keyword in lowered for keyword in keywords):
                    return candidate
        lowered_title = title.lower()
        for candidate, keywords in STATUS_KEYWORDS:
            if any(keyword in lowered_title for keyword in keywords):
                return candidate
        if closing_at is not None:
            return OpportunityStatus.OPEN if closing_at >= utcnow() else OpportunityStatus.CLOSED
        return OpportunityStatus.UNKNOWN

    def _briefing_required(
        self, item: RawItem, briefing_date: Any, briefing_location: str | None
    ) -> bool | None:
        explicit = item.get("briefing_required")
        if isinstance(explicit, bool):
            return explicit
        if isinstance(explicit, str):
            lowered = explicit.strip().lower()
            if lowered in {"yes", "true", "y", "compulsory", "mandatory"}:
                return True
            if lowered in {"no", "false", "n", "none", "not applicable", "n/a"}:
                return False
        if briefing_date or briefing_location:
            return True
        return None

    def _briefing_compulsory(self, item: RawItem) -> bool | None:
        blob = " ".join(
            str(item.fields.get(key, ""))
            for key in ("briefing_required", "briefing_location", "briefing", "description")
        ).lower()
        if not any(word in blob for word in BRIEFING_KEYWORDS):
            return None
        if any(word in blob for word in BRIEFING_COMPULSORY_KEYWORDS):
            return True
        if "non-compulsory" in blob or "not compulsory" in blob or "optional" in blob:
            return False
        return None

    def _contact(self, item: RawItem) -> dict[str, Any] | None:
        name = clean_text(item.get("contact_name", "contact_person", "contact"))
        email = clean_text(item.get("contact_email", "email"))
        phone = clean_text(item.get("contact_phone", "telephone", "phone"))

        if not email:
            blob = " ".join(
                str(item.fields.get(key, "")) for key in ("description", "contact", "details")
            )
            emails = extract_emails(blob)
            email = emails[0] if emails else None
        if not phone:
            blob = " ".join(
                str(item.fields.get(key, "")) for key in ("description", "contact", "details")
            )
            phones = extract_phones(blob)
            phone = phones[0] if phones else None

        if not any((name, email, phone)):
            return None
        return {
            "name": name,
            "email": email.lower() if email else None,
            "phone": phone,
            "role": clean_text(item.get("contact_role")),
            "organization": clean_text(item.get("organization")),
        }

    def _raw_payload(self, item: RawItem) -> dict[str, Any] | None:
        payload = dict(item.raw_payload or {})
        payload.setdefault("fields", dict(item.fields))
        if item.raw_html:
            payload["html_length"] = len(item.raw_html)
        return payload or None

    def _confidence(
        self, published_confidence: float, closing_confidence: float, reference: str | None
    ) -> float:
        score = 0.5
        score += 0.2 * published_confidence
        score += 0.2 * closing_confidence
        if reference:
            score += 0.1
        return round(min(1.0, score), 3)
