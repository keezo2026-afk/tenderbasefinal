"""Unit tests for document hashing, storage keys, format sniffing and extraction."""

from __future__ import annotations

import io

import pytest

from app.documents.classifier import DocumentClassifier
from app.documents.downloader import sniff_format
from app.documents.extractor import TextExtractor
from app.documents.storage import LocalBlobStorage, build_storage_key
from app.enums import DocumentFormat, DocumentType, ExtractionMethod
from app.errors import DocumentError, ExtractionError
from app.utils.hashing import sha256_bytes


def make_pdf(text: str = "TEST FIXTURE tender advert") -> bytes:
    """Build a tiny valid PDF in memory (no fixture binary needed)."""
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# --- storage keys ---------------------------------------------------------


def test_storage_key_is_content_addressed_and_sharded():
    digest = sha256_bytes(b"content")
    key = build_storage_key(namespace="documents", digest=digest, extension="pdf")
    assert key == f"documents/{digest[:2]}/{digest[2:4]}/{digest}.pdf"


def test_storage_key_sanitises_hostile_input():
    digest = sha256_bytes(b"content")
    key = build_storage_key(namespace="../../etc", digest=digest, extension="../sh")
    assert ".." not in key
    with pytest.raises(DocumentError):
        build_storage_key(namespace="documents", digest="not-a-digest")


def test_local_storage_round_trip_and_traversal_guard(tmp_path):
    storage = LocalBlobStorage(tmp_path)
    key = build_storage_key(namespace="documents", digest=sha256_bytes(b"payload"))
    storage.write_bytes(key, b"payload")
    assert storage.exists(key)
    assert storage.read_bytes(key) == b"payload"
    assert storage.size(key) == 7

    with pytest.raises(DocumentError):
        storage.read_bytes("../../../etc/passwd")

    storage.delete(key)
    assert not storage.exists(key)


# --- format sniffing ------------------------------------------------------


def test_sniff_format_trusts_bytes_over_filename():
    fmt, mime = sniff_format(b"%PDF-1.7\n...", filename="invoice.docx")
    assert fmt is DocumentFormat.PDF
    assert mime == "application/pdf"


def test_sniff_format_detects_ooxml_from_declared_type():
    fmt, _ = sniff_format(
        b"PK\x03\x04rest",
        declared_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert fmt is DocumentFormat.DOCX


def test_sniff_format_falls_back_to_unknown():
    fmt, _ = sniff_format(b"\x01\x02\x03")
    assert fmt is DocumentFormat.UNKNOWN


# --- extraction -----------------------------------------------------------


def test_native_pdf_extraction_is_attempted_and_ocr_stays_off_by_default():
    result = TextExtractor().extract(make_pdf(), document_format=DocumentFormat.PDF)
    assert result.method is ExtractionMethod.NATIVE_PDF
    assert result.ocr_used is False
    assert result.page_count == 1


def test_plain_text_and_csv_extraction():
    extractor = TextExtractor()
    text = extractor.extract(b"hello world", document_format=DocumentFormat.TXT)
    assert text.text == "hello world"
    assert text.method is ExtractionMethod.PLAIN_TEXT

    csv_result = extractor.extract(b"a,b\n1,2\n", document_format=DocumentFormat.CSV)
    assert "a | b" in csv_result.text
    assert csv_result.method is ExtractionMethod.SPREADSHEET


def test_html_extraction_strips_scripts():
    result = TextExtractor().extract(
        b"<html><body><script>bad()</script><p>Tender notice</p></body></html>",
        document_format=DocumentFormat.HTML,
    )
    assert "bad()" not in result.text
    assert "Tender notice" in result.text


def test_extraction_rejects_empty_content():
    with pytest.raises(ExtractionError):
        TextExtractor().extract(b"")


# --- classification -------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("addendum-1.pdf", DocumentType.ADDENDUM),
        ("SBD 4 declaration form.pdf", DocumentType.BID_FORM),
        ("technical-specification.pdf", DocumentType.SPECIFICATION),
        ("briefing-minutes.pdf", DocumentType.BRIEFING_NOTES),
        ("award-notice.pdf", DocumentType.AWARD_NOTICE),
        ("random-file.bin", DocumentType.UNKNOWN),
    ],
)
def test_rule_based_classification(filename, expected):
    result = DocumentClassifier().classify(filename=filename)
    assert result.document_type is expected


def test_classification_uses_text_when_filename_is_uninformative():
    result = DocumentClassifier().classify(
        filename="doc1.pdf", text="REQUEST FOR QUOTATION for cleaning services"
    )
    assert result.document_type is DocumentType.RFQ_DOCUMENT
    assert 0 < result.confidence <= 1
