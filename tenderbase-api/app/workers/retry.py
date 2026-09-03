"""Retry policy for ARQ ingestion jobs.

ARQ re-runs a failed job on its next poll — immediately. For a queue worker
pulling database rows that is fine; for a municipal website returning 502 under
load it is a denial-of-service attempt dressed up as resilience. The pipeline
already classifies every error it hits (``retryable`` on :class:`StageError`,
derived from the exception class), so this module turns that classification into a
queue-level decision:

* **transient** — at least one retryable error and no non-retryable "this is a
  configuration fact" error → re-arm the job with a deferred score so the next
  try happens after an exponential backoff with jitter, until
  ``WORKER_MAX_TRIES`` is exhausted;
* **permanent** — only non-retryable errors (unknown connector, unsafe URL,
  robots refusal) → fail now. Retrying a misconfiguration does not fix it, it
  just hides the error behind three more log lines.

The deferral is expressed by raising :class:`arq.worker.Retry`, ARQ's supported
way to say "run this again later"; hand-rolling a re-enqueue would lose ARQ's job
bookkeeping (attempt counter, result row, abort support).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, NoReturn

from app.logging import get_logger
from app.utils.backoff import exponential_backoff_seconds
from app.utils.dates import utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.config import Settings
    from app.db.models.ingestion import IngestionJob
    from app.db.models.source import SourceRun

logger = get_logger("tenderbase.workers.retry")

#: A deferred retry may wait a while, but it must not start looking like a
#: scheduled re-crawl (that is what ``crawl_frequency_minutes`` is for).
MAX_RETRY_DELAY_SECONDS = 900.0


@dataclass(frozen=True)
class RetryDecision:
    """What the worker should do with a failed run."""

    retry: bool
    delay_seconds: float
    attempts_used: int
    attempts_allowed: int
    reason: str

    @property
    def delay(self) -> timedelta:
        return timedelta(seconds=self.delay_seconds)


def decide_retry(
    run: SourceRun,
    *,
    job_try: int,
    settings: Settings,
) -> RetryDecision:
    """Classify ``run`` and compute the deferral for the next attempt.

    ``job_try`` is ARQ's 1-based attempt counter (``ctx["job_try"]``). It is
    clamped to at least 1 so a task invoked outside a worker — a script, a test,
    a manual ``await`` — behaves like a first attempt instead of a negative one.
    """
    attempts_allowed = max(1, int(settings.worker_max_tries))
    attempts_used = max(1, int(job_try))

    errors = _errors(run)
    if not errors:
        # Nothing recorded a reason; the source answered but produced no usable
        # data. One more attempt is cheap, an infinite loop is not.
        transient = True
    else:
        transient = any(error.get("retryable") for error in errors)

    if not transient:
        return RetryDecision(False, 0.0, attempts_used, attempts_allowed, "permanent_failure")
    if attempts_used >= attempts_allowed:
        return RetryDecision(False, 0.0, attempts_used, attempts_allowed, "retries_exhausted")

    delay = exponential_backoff_seconds(
        attempts_used - 1,
        base_seconds=settings.worker_retry_backoff_seconds,
        max_seconds=MAX_RETRY_DELAY_SECONDS,
    )
    return RetryDecision(True, delay, attempts_used, attempts_allowed, "transient_failure")


async def defer_or_fail(
    ctx: dict,
    *,
    run: SourceRun,
    job: IngestionJob | None,
    settings: Settings,
    session: object = None,
) -> NoReturn:
    """Record the decision on the job row, then raise the ARQ control-flow error.

    The row is committed *before* raising: the exception unwinds
    ``session_scope()``, which rolls back, and an operator reading
    ``ingestion_jobs`` after a retry storm needs to see the attempts and the
    message — not a row frozen in ``RUNNING`` forever.
    """
    from arq.worker import JobExecutionFailed, Retry

    decision = decide_retry(run, job_try=int(ctx.get("job_try", 1)), settings=settings)

    if job is not None:
        if decision.retry:
            job.status = "RETRYING"
            job.scheduled_for = utcnow() + decision.delay
            job.completed_at = None
        else:
            job.status = "FAILED"
            job.completed_at = utcnow()
        job.error_message = (_first_error_message(run) or decision.reason)[:2000]
        if session is not None:
            await session.commit()  # type: ignore[attr-defined]

    logger.warning(
        "job.retry_decision",
        source_run_id=str(run.id),
        job_id=str(job.id) if job is not None else None,
        source_id=str(run.source_id),
        decision=decision.reason,
        retry=decision.retry,
        delay_seconds=round(decision.delay_seconds, 2),
        attempts=f"{decision.attempts_used}/{decision.attempts_allowed}",
    )
    if decision.retry:
        raise Retry(decision.delay)
    raise JobExecutionFailed(decision.reason)


def _errors(run: SourceRun) -> list[dict]:
    stats = run.stats or {}
    errors = stats.get("errors")
    return [error for error in errors if isinstance(error, dict)] if errors else []


def _first_error_message(run: SourceRun) -> str:
    errors = _errors(run)
    if errors:
        message = errors[0].get("message")
        if message:
            return str(message)
    return run.error_message or ""
