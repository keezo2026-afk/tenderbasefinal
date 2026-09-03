"""Worker tasks.

Each task is small, idempotent and isolated: a failure in one source or one
document never aborts the batch. Every task is safe to run twice — ingestion is
keyed on (source, external id) and document processing checks the content hash —
which is what makes ARQ's retry-then-defer strategy in :mod:`app.workers.retry`
affordable.

Tasks come in two flavours:

* ``ingest_source`` is per-source work with a row in ``ingestion_jobs``, so it
  can be retried with a backoff and must record *why* it gave up;
* the cron tasks (``schedule_due_sources``, ``process_documents``,
  ``monitor_source_health``) are self-rescheduling. Retrying one of those is
  pointless — the schedule *is* the retry — so they log and return counters
  instead of raising.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.config import get_settings
from app.db.models.ingestion import IngestionJob
from app.db.models.source import MunicipalitySource
from app.db.session import session_scope
from app.documents.downloader import DocumentDownloader
from app.enums import JobStatus, JobTrigger
from app.ingestion.discovery import find_due_sources
from app.ingestion.fetcher import HTTPFetcher
from app.ingestion.pipeline import IngestionPipeline
from app.logging import get_logger, job_id_ctx
from app.services.document_service import DocumentService
from app.utils.dates import utcnow
from app.workers.retry import defer_or_fail

logger = get_logger("tenderbase.workers.tasks")

DEFAULT_DOCUMENT_BATCH = 25


async def ingest_source(ctx: dict[str, Any], source_id: str, *, job_id: str | None = None) -> dict:
    """Run the ingestion pipeline for a single source."""
    token = job_id_ctx.set(job_id)
    try:
        async with session_scope() as session:
            source = (
                (
                    await session.execute(
                        select(MunicipalitySource).where(MunicipalitySource.id == UUID(source_id))
                    )
                )
                .scalars()
                .first()
            )
            if source is None:
                logger.warning("task.source_missing", source_id=source_id)
                return {"status": "missing", "source_id": source_id}

            job = None
            if job_id:
                job = (
                    (
                        await session.execute(
                            select(IngestionJob).where(IngestionJob.id == UUID(job_id))
                        )
                    )
                    .scalars()
                    .first()
                )
            if job is None:
                job = IngestionJob(
                    source_id=source.id,
                    job_type="SOURCE_INGEST",
                    status=str(JobStatus.RUNNING),
                    trigger=str(JobTrigger.SCHEDULER),
                    started_at=utcnow(),
                )
                session.add(job)
                await session.flush()
            else:
                job.status = str(JobStatus.RUNNING)
                job.started_at = utcnow()
                job.attempt += 1

            try:
                async with HTTPFetcher() as fetcher:
                    pipeline = IngestionPipeline(fetcher=fetcher)
                    run = await pipeline.run_source(session, source, job=job, commit=False)
            except Exception as exc:  # noqa: BLE001 - the queue owns this retry
                # The pipeline isolates *source* failures and reports them as data;
                # anything that escapes it is an infrastructure fault (database,
                # event loop, a bug). Defer and try again rather than losing the job.
                await session.rollback()
                if job is not None:
                    job.status = str(JobStatus.RETRYING)
                    job.error_message = f"{type(exc).__name__}: {exc}"[:2000]
                await session.commit()
                logger.exception("task.ingest_crashed", source_id=source_id, error=str(exc))
                raise _defer(ctx, base=get_settings().worker_retry_backoff_seconds) from exc

            if run.status == str(JobStatus.FAILED):
                # Failures are data at this level; convert them into a queue-level
                # decision (backoff + jitter, or give up) before returning.
                await defer_or_fail(ctx, run=run, job=job, settings=get_settings(), session=session)

            await _record_queue_attempt(ctx, job)

            return {
                "status": run.status,
                "source_id": source_id,
                "run_id": str(run.id),
                "items_found": run.items_found,
                "items_created": run.items_created,
                "items_updated": run.items_updated,
            }
    finally:
        job_id_ctx.reset(token)


def _defer(ctx: dict[str, Any], *, base: float) -> Exception:
    """Build the ARQ "run this later" control-flow error for the next attempt."""
    from arq.worker import Retry

    from app.utils.backoff import exponential_backoff_seconds

    delay = exponential_backoff_seconds(
        max(0, int(ctx.get("job_try", 1)) - 1), base_seconds=base, max_seconds=900.0
    )
    return Retry(timedelta(seconds=delay))


async def _record_queue_attempt(ctx: dict[str, Any], job: IngestionJob | None) -> None:
    """Stamp the ARQ attempt count onto the job row once a run succeeds."""
    if job is None:
        return
    job.attempt = max(int(job.attempt or 0), int(ctx.get("job_try", 1)))


async def schedule_due_sources(ctx: dict[str, Any], limit: int = 25) -> dict:
    """Queue ingestion jobs for every source whose crawl interval has elapsed."""
    from app.workers.queue import get_queue

    queued: list[str] = []
    async with session_scope() as session:
        sources = await find_due_sources(session, limit=limit)
        queue = get_queue()
        for source in sources:
            job = IngestionJob(
                source_id=source.id,
                job_type="SOURCE_INGEST",
                status=str(JobStatus.QUEUED),
                trigger=str(JobTrigger.SCHEDULER),
                scheduled_for=utcnow(),
            )
            session.add(job)
            await session.flush()
            try:
                # No ``unique_id`` here on purpose: ARQ keeps a job's payload
                # until ``keep_result`` expires, so a permanent id would make
                # every later scheduled run of this source a silent no-op.
                job.queue_job_id = await queue.enqueue(
                    "ingest_source", str(source.id), job_id=str(job.id)
                )
            except Exception as exc:  # noqa: BLE001 - one enqueue failure is isolated
                job.status = str(JobStatus.FAILED)
                job.error_message = str(exc)[:2000]
                logger.warning("task.enqueue_failed", source_id=str(source.id), error=str(exc))
                continue
            queued.append(str(source.id))
    logger.info("task.scheduled", queued=len(queued))
    return {"queued": len(queued), "source_ids": queued}


async def process_documents(ctx: dict[str, Any], limit: int = DEFAULT_DOCUMENT_BATCH) -> dict:
    """Download, hash, store, extract and classify pending documents."""
    processed = 0
    failed = 0
    async with session_scope() as session:
        service = DocumentService(session)
        pending = await service.pending_downloads(limit=limit)
        if not pending:
            return {"processed": 0, "failed": 0}
        async with HTTPFetcher() as fetcher:
            downloader = DocumentDownloader(fetcher=fetcher, settings=get_settings())
            for document in pending:
                try:
                    await service.process_document(document, downloader=downloader)
                    processed += int(document.is_downloaded)
                    failed += int(not document.is_downloaded)
                except Exception as exc:  # noqa: BLE001 - isolate per document
                    failed += 1
                    logger.warning(
                        "task.document_failed", document_id=str(document.id), error=str(exc)
                    )
    logger.info("task.documents_processed", processed=processed, failed=failed)
    return {"processed": processed, "failed": failed}


async def monitor_source_health(ctx: dict[str, Any]) -> dict:
    """Report sources that need engineering attention."""
    from app.ingestion.discovery import find_sources_needing_attention

    async with session_scope() as session:
        sources = await find_sources_needing_attention(session)
        for source in sources:
            logger.warning(
                "source.needs_attention",
                source_id=str(source.id),
                name=source.name,
                consecutive_failures=source.consecutive_failures,
                health_status=source.health_status,
                last_success_at=str(source.last_success_at),
            )
    return {"unhealthy_sources": len(sources)}
