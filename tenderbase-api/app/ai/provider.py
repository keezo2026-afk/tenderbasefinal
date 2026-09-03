"""AI enrichment abstraction.

AI is an **optional, asynchronous enrichment stage**. The core API and the
ingestion pipeline start and run correctly with no AI credentials configured;
in that case :func:`get_ai_provider` returns the :class:`NullAIProvider`, which
reports itself unavailable rather than pretending to produce results.

Nothing here is coupled to a specific vendor: providers implement the
:class:`AIProvider` interface and are selected by the ``AI_PROVIDER`` setting.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings, get_settings
from app.errors import AIUnavailableError
from app.logging import get_logger

logger = get_logger("tenderbase.ai")


@dataclass(slots=True)
class Summary:
    """A generated summary with provenance."""

    text: str
    model: str
    provider: str
    tokens: int | None = None


@dataclass(slots=True)
class ExtractedRequirements:
    """Structured facts extracted from a procurement document.

    Every field is optional: the extractor must return ``None``/empty rather
    than inventing requirements that are not in the source text.
    """

    requirements: list[str] = field(default_factory=list)
    eligibility: list[str] = field(default_factory=list)
    mandatory_documents: list[str] = field(default_factory=list)
    key_dates: dict[str, str] = field(default_factory=dict)
    scope_of_work: str | None = None
    estimated_value: str | None = None
    risk_flags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    model: str | None = None
    provider: str | None = None


@dataclass(slots=True)
class ClassificationResult:
    """AI classification output."""

    industry: str | None = None
    category: str | None = None
    location: str | None = None
    confidence: float = 0.0
    model: str | None = None
    provider: str | None = None


class AIProvider(ABC):
    """Interface every AI provider implements."""

    name: str = "abstract"

    def __init__(self, settings: Settings | None = None) -> None:
        # Declared here so that `PROVIDERS[name](settings)` type-checks for every
        # entry, including the null provider that needs no configuration at all.
        self._settings = settings

    @property
    @abstractmethod
    def available(self) -> bool:
        """Whether this provider is configured and usable."""

    @abstractmethod
    async def summarize(self, text: str, *, max_words: int = 150) -> Summary: ...

    @abstractmethod
    async def extract_requirements(self, text: str) -> ExtractedRequirements: ...

    @abstractmethod
    async def classify(
        self, text: str, *, taxonomy: list[str] | None = None
    ) -> ClassificationResult: ...


class NullAIProvider(AIProvider):
    """Explicitly unavailable provider used when AI is disabled.

    Calling it raises :class:`AIUnavailableError` — enrichment is skipped and
    recorded, never faked.
    """

    name = "null"

    @property
    def available(self) -> bool:
        return False

    async def summarize(self, text: str, *, max_words: int = 150) -> Summary:
        raise AIUnavailableError("AI enrichment is disabled (AI_ENABLED=false)")

    async def extract_requirements(self, text: str) -> ExtractedRequirements:
        raise AIUnavailableError("AI enrichment is disabled (AI_ENABLED=false)")

    async def classify(
        self, text: str, *, taxonomy: list[str] | None = None
    ) -> ClassificationResult:
        raise AIUnavailableError("AI enrichment is disabled (AI_ENABLED=false)")


class HTTPAIProvider(AIProvider):
    """Base class for HTTP-based providers (OpenAI, Anthropic, ...).

    The request/response mapping for each vendor is intentionally left
    unimplemented: wiring it requires credentials and live verification, which
    are out of scope for this build. The integration boundary — configuration,
    interface, error handling and the optional-by-default contract — is
    complete, so adding a vendor is a self-contained change.
    """

    name = "http"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def available(self) -> bool:
        return bool(self.settings.ai_enabled and self.settings.ai_api_key)

    async def summarize(self, text: str, *, max_words: int = 150) -> Summary:
        raise NotImplementedError(
            f"The '{self.name}' provider integration is not implemented yet. "
            "Implement the request mapping in app/ai/provider.py."
        )

    async def extract_requirements(self, text: str) -> ExtractedRequirements:
        raise NotImplementedError(f"The '{self.name}' provider integration is not implemented yet.")

    async def classify(
        self, text: str, *, taxonomy: list[str] | None = None
    ) -> ClassificationResult:
        raise NotImplementedError(f"The '{self.name}' provider integration is not implemented yet.")


class OpenAIProvider(HTTPAIProvider):
    """OpenAI-compatible provider (integration pending credentials)."""

    name = "openai"


class AnthropicProvider(HTTPAIProvider):
    """Anthropic provider (integration pending credentials)."""

    name = "anthropic"


PROVIDERS: dict[str, type[AIProvider]] = {
    "null": NullAIProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
}


def get_ai_provider(settings: Settings | None = None) -> AIProvider:
    """Return the configured provider — never raises at import/startup time."""
    cfg = settings or get_settings()
    if not cfg.ai_enabled:
        return NullAIProvider()
    provider_cls = PROVIDERS.get(cfg.ai_provider, NullAIProvider)
    provider = provider_cls(cfg)
    if not provider.available:
        logger.warning("ai.provider_unavailable", provider=cfg.ai_provider)
        return NullAIProvider()
    return provider


def ai_status(settings: Settings | None = None) -> dict[str, Any]:
    """Report AI availability (used by diagnostics and documentation)."""
    cfg = settings or get_settings()
    provider = get_ai_provider(cfg)
    return {
        "enabled": cfg.ai_enabled,
        "provider": provider.name,
        "available": provider.available,
        "model": cfg.ai_model,
    }
