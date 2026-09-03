"""OCR abstraction.

OCR is **optional and off by default** (``OCR_ENABLED=false``): it is slow and
expensive, and most South African procurement PDFs contain a native text layer.

The engine below wraps Tesseract via ``pytesseract``/``pdf2image``. When those
optional dependencies (or the Tesseract binary) are missing, ``available`` is
``False`` and the extractor simply keeps the native result — no silent
substitution, no crash.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.config import Settings, get_settings
from app.logging import get_logger

logger = get_logger("tenderbase.documents.ocr")

#: Hard ceiling on pages sent to OCR in one call.
MAX_OCR_PAGES = 20
OCR_DPI = 200


class OCREngine(ABC):
    """Interface every OCR backend implements."""

    name: str = "abstract"

    @property
    @abstractmethod
    def available(self) -> bool:
        """Whether this engine can actually run in the current environment."""

    @abstractmethod
    def extract(self, data: bytes) -> tuple[str, float | None]:
        """Return ``(text, confidence)`` for a document's bytes."""


class NullOCREngine(OCREngine):
    """Explicitly unavailable engine used when OCR is disabled."""

    name = "null"

    @property
    def available(self) -> bool:
        return False

    def extract(self, data: bytes) -> tuple[str, float | None]:
        return "", None


class TesseractOCREngine(OCREngine):
    """Tesseract-backed OCR for image-only PDFs."""

    name = "tesseract"

    def __init__(self, *, languages: str = "eng", max_pages: int = MAX_OCR_PAGES) -> None:
        self.languages = languages
        self.max_pages = max_pages

    @property
    def available(self) -> bool:
        try:
            import pytesseract  # noqa: F401
            from pdf2image import convert_from_bytes  # noqa: F401
        except ImportError:
            return False
        try:
            import pytesseract

            pytesseract.get_tesseract_version()
        except Exception:  # noqa: BLE001 - binary missing or misconfigured
            return False
        return True

    def extract(
        self, data: bytes
    ) -> tuple[str, float | None]:  # pragma: no cover - needs tesseract
        import pytesseract
        from pdf2image import convert_from_bytes

        images = convert_from_bytes(data, dpi=OCR_DPI, first_page=1, last_page=self.max_pages)
        chunks: list[str] = []
        confidences: list[float] = []
        for image in images:
            chunks.append(pytesseract.image_to_string(image, lang=self.languages))
            try:
                report = pytesseract.image_to_data(
                    image, lang=self.languages, output_type=pytesseract.Output.DICT
                )
                values = [float(c) for c in report.get("conf", []) if str(c).lstrip("-").isdigit()]
                positive = [v for v in values if v >= 0]
                if positive:
                    confidences.append(sum(positive) / len(positive) / 100.0)
            except Exception:  # noqa: BLE001 - confidence is best-effort
                pass
        confidence = sum(confidences) / len(confidences) if confidences else None
        return "\n\n".join(chunks), confidence


def get_ocr_engine(settings: Settings | None = None) -> OCREngine:
    """Return the configured OCR engine (null engine when disabled)."""
    cfg = settings or get_settings()
    if not cfg.ocr_enabled:
        return NullOCREngine()
    engine = TesseractOCREngine(languages=cfg.ocr_languages)
    if not engine.available:
        logger.warning(
            "ocr.unavailable",
            detail="OCR_ENABLED is true but pytesseract/pdf2image/tesseract are not installed",
        )
        return NullOCREngine()
    return engine
