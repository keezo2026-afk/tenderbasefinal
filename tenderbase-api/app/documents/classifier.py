"""Rule-based document classification.

Deterministic keyword rules run first and always: they are free, explainable
and sufficient for the common South African bid-pack document types. AI-based
classification (see :mod:`app.ai`) can refine this later, but is never required.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.enums import DocumentType
from app.utils.text import clean_text

#: (document type, keywords, weight) — matched against filename + title + text.
RULES: tuple[tuple[DocumentType, tuple[str, ...], float], ...] = (
    (
        DocumentType.ADDENDUM,
        ("addendum", "amendment", "erratum", "corrigendum", "notice of change"),
        0.9,
    ),
    (
        DocumentType.AWARD_NOTICE,
        ("award", "awarded bidder", "successful bidder", "appointment of"),
        0.9,
    ),
    (
        DocumentType.BRIEFING_NOTES,
        ("briefing", "site meeting", "compulsory meeting", "minutes of the briefing"),
        0.85,
    ),
    (
        DocumentType.BID_FORM,
        ("sbd 1", "sbd1", "sbd 4", "sbd 6", "returnable", "bid form", "declaration form"),
        0.8,
    ),
    (
        DocumentType.SPECIFICATION,
        ("specification", "scope of work", "terms of reference", "bill of quantities", "boq"),
        0.8,
    ),
    (DocumentType.RFQ_DOCUMENT, ("rfq", "request for quotation", "quotation document"), 0.8),
    (
        DocumentType.TENDER_DOCUMENT,
        ("tender document", "bid document", "rfp", "rfb", "tender pack"),
        0.75,
    ),
    (
        DocumentType.ADVERT,
        ("advert", "advertisement", "invitation to bid", "notice to bidders"),
        0.7,
    ),
)

#: How much of the extracted text is inspected (the front matter is enough).
TEXT_WINDOW = 4000


@dataclass(frozen=True, slots=True)
class Classification:
    """A classification decision with its confidence and evidence."""

    document_type: DocumentType
    confidence: float
    matched_keyword: str | None = None
    method: str = "RULE"


class DocumentClassifier:
    """Assigns a :class:`DocumentType` from filename, title and text."""

    def classify(
        self,
        *,
        filename: str | None = None,
        title: str | None = None,
        text: str | None = None,
    ) -> Classification:
        """Classify a document; returns ``UNKNOWN`` when nothing matches."""
        haystacks = [
            (clean_text(filename) or "").lower(),
            (clean_text(title) or "").lower(),
            (clean_text((text or "")[:TEXT_WINDOW]) or "").lower(),
        ]
        # Filename and title are stronger evidence than body text.
        weights = (1.0, 0.95, 0.8)

        best: Classification | None = None
        for document_type, keywords, base in RULES:
            for haystack, weight in zip(haystacks, weights, strict=True):
                if not haystack:
                    continue
                for keyword in keywords:
                    if keyword in haystack:
                        score = round(base * weight, 3)
                        if best is None or score > best.confidence:
                            best = Classification(document_type, score, keyword)
                        break
        return best or Classification(DocumentType.UNKNOWN, 0.0)
