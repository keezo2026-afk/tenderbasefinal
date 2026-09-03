"""Parser stage helpers.

The parsing *logic* lives in each connector (it is inherently source-shaped).
This module provides the stage wrapper the pipeline uses: it runs a connector's
item stream, isolates per-item failures and yields parsed items together with
the errors that occurred, so a single malformed row never aborts a run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.connectors.base import ProcurementConnector, RawItem, SourceContext
from app.enums import ErrorStage
from app.errors import TenderBaseError
from app.logging import get_logger

logger = get_logger("tenderbase.parser")

#: Hard ceiling per run, protecting against runaway pagination loops.
MAX_ITEMS_PER_RUN = 5_000


@dataclass(slots=True)
class StageError:
    """A recoverable failure at one pipeline stage."""

    stage: ErrorStage
    code: str
    message: str
    url: str | None = None
    retryable: bool = False
    context: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        """JSON-safe projection, stored in ``source_runs.stats`` and the job result."""
        return {
            "stage": str(self.stage),
            "code": self.code,
            "message": self.message[:500],
            "url": self.url,
            "retryable": self.retryable,
        }

    @classmethod
    def from_exception(
        cls, exc: Exception, *, stage: ErrorStage, url: str | None = None
    ) -> StageError:
        if isinstance(exc, TenderBaseError):
            return cls(
                stage=stage,
                code=exc.code,
                message=str(exc)[:2000],
                url=url,
                retryable=bool(getattr(exc, "retryable", False)),
                context=dict(exc.details),
            )
        return cls(
            stage=stage,
            code=type(exc).__name__.upper(),
            message=str(exc)[:2000],
            url=url,
            retryable=False,
        )


async def parse_source(
    connector: ProcurementConnector,
    source: SourceContext,
    *,
    max_items: int = MAX_ITEMS_PER_RUN,
    errors: list[StageError] | None = None,
) -> AsyncIterator[RawItem]:
    """Yield raw items from a connector, collecting failures instead of raising.

    A failure inside the connector's async generator terminates that generator
    (it cannot be resumed), so it is recorded once and the run ends cleanly —
    other sources are unaffected.
    """
    sink = errors if errors is not None else []
    count = 0
    iterator = connector.run(source)
    while True:
        try:
            item = await anext(iterator)  # type: ignore[arg-type]
        except StopAsyncIteration:
            break
        except Exception as exc:  # noqa: BLE001 - isolate connector failures
            stage = _stage_for(exc)
            sink.append(StageError.from_exception(exc, stage=stage, url=source.base_url))
            logger.warning(
                "parser.stage_failed",
                source_id=source.id,
                connector=connector.key,
                stage=str(stage),
                error=str(exc),
            )
            break

        count += 1
        if count > max_items:
            sink.append(
                StageError(
                    stage=ErrorStage.PARSE,
                    code="MAX_ITEMS_EXCEEDED",
                    message=f"Run stopped after {max_items} items",
                    url=source.base_url,
                )
            )
            logger.warning("parser.max_items", source_id=source.id, max_items=max_items)
            break
        yield item


def _stage_for(exc: Exception) -> ErrorStage:
    code = getattr(exc, "code", "")
    if code.startswith("FETCH") or code in {
        "ROBOTS_DISALLOWED",
        "RESPONSE_TOO_LARGE",
        "UNSAFE_URL",
    }:
        return ErrorStage.FETCH
    if code == "PARSE_ERROR":
        return ErrorStage.PARSE
    if code == "CONNECTOR_NOT_REGISTERED":
        return ErrorStage.DISCOVERY
    return ErrorStage.UNKNOWN
