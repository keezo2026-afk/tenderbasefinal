"""Live queue tests: ARQ, Redis and the worker task bodies.

These are the only tests in the suite that talk to a broker, and they are the only
proof that the ingestion worker actually runs: the unit tests cover the decision
logic, and here the jobs go through ``arq``'s own worker loop, are dequeued, and
write real rows.

They skip (loudly) unless something answers on ``REDIS_URL``, and always use
database 15, flushing it between tests — never the developer's database 0.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db.models.ingestion import IngestionError, IngestionJob
from app.db.models.opportunity import ProcurementOpportunity
from app.db.models.source import SourceRun
from app.enums import HealthStatus, JobStatus, JobTrigger, SourceLifecycle
from app.utils.dates import utcnow
from app.workers import tasks as worker_tasks
from app.workers.queue import QUEUE_NAME, JobQueue

pytestmark = pytest.mark.redis

async def reload_row(session, model, row_id):  # noqa: ANN001, ANN201 - test helper
    """Re-read a row from the database, not from the session's identity map.

    The fixtures here hold their own session, and the *task* commits through a
    different one; without ``populate_existing`` SQLAlchemy hands back the
    stale object it already loaded and the assertion below would be a lie.
    """
    stmt = (
        select(model).where(model.id == row_id).execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalars().one()


def as_utc(value):
    """Normalise a timestamp read back from either backend.

    PostgreSQL hands back an aware ``datetime`` for ``TIMESTAMPTZ``, SQLite hands
    back a naive one; comparing the two to ``utcnow()`` otherwise blows up on
    exactly one of the two dialects.
    """
    from datetime import UTC

    return value if value.tzinfo else value.replace(tzinfo=UTC)


@pytest.fixture
def queue_settings(redis_url) -> Settings:
    """Worker settings aimed at a throwaway Redis database (``queue`` flushes it
    between tests, so test order never matters)."""
    if not redis_url:
        pytest.skip("no Redis answering on REDIS_URL; queue tests need one")
    return Settings(
        app_env="test",
        redis_url=redis_url,
        worker_max_tries=3,
        worker_retry_backoff_seconds=0.1,
    )


@pytest.fixture
async def queue(queue_settings):  # noqa: ANN201 - async CM yielding a JobQueue
    """A JobQueue bound to the throwaway database, flushed before each test."""
    instance = JobQueue(queue_settings)
    pool = await instance.connect()
    await pool.flushdb()
    try:
        yield instance
    finally:
        await instance.close()


async def test_health_reports_live_redis(queue):
    assert await queue.health() is True


async def test_enqueued_job_is_visible_to_arq_and_stays_queued(queue):
    """An enqueued job is stored by ARQ, not executed until a worker claims it."""
    from arq.jobs import Job, JobStatus

    job_id = await queue.enqueue("monitor_source_health")
    assert job_id

    pool = await queue.connect()
    job = Job(job_id, pool, _queue_name=QUEUE_NAME)
    assert await job.status() == JobStatus.queued
    # Nothing ran: no worker was alive when the assertion was made, and no
    # result row exists yet.
    assert await pool.get(f"arq:result:{job_id}") is None


async def test_reusing_a_job_id_does_not_queue_the_work_twice(queue):
    """Idempotent enqueue: this is what stops the scheduler double-queuing a source."""
    first = await queue.enqueue("ingest_source", "source-id", unique_id="fixed-id")
    second = await queue.enqueue("ingest_source", "source-id", unique_id="fixed-id")
    assert first == "fixed-id"
    assert second is None
    # ARQ keeps the queue itself as a zset of job ids scored by due time.
    pool = await queue.connect()
    assert await pool.zcard(QUEUE_NAME) == 1


# --- the worker loop ------------------------------------------------------


@pytest.fixture
def listing_routes(fixture_loader):
    return {
        "https://example.org/tenders": (200, fixture_loader("html_listing.html"), "text/html"),
    }


@pytest.fixture
def stub_fetcher(monkeypatch, mock_fetcher, listing_routes):
    """Replace the task's HTTPFetcher with the canned-response one.

    The task constructs its own fetcher (that is how a worker gets a fresh client
    per job), so this is the seam that keeps the test off the network.
    """
    fetcher = mock_fetcher(listing_routes)
    monkeypatch.setattr(worker_tasks, "HTTPFetcher", lambda *args, **kwargs: fetcher)
    return fetcher


async def test_arq_worker_runs_an_ingestion_job_end_to_end(
    queue, queue_settings, session, source, worker_database, stub_fetcher, monkeypatch
):
    """Enqueue through Redis, let arq's worker run it, assert the rows."""
    from arq.worker import create_worker

    from app.workers.queue import redis_settings
    from app.workers.scheduler import WorkerSettings

    job = IngestionJob(
        source_id=source.id,
        job_type="SOURCE_INGEST",
        status=str(JobStatus.QUEUED),
        trigger=str(JobTrigger.MANUAL),
    )
    session.add(job)
    await session.commit()

    queue_job_id = await queue.enqueue("ingest_source", str(source.id), job_id=str(job.id))
    assert queue_job_id

    # arq's own factory, pointed at the throwaway database, in burst mode: one
    # pass over the queue, then it stops. on_startup/on_shutdown are dropped
    # because they would reconfigure logging and dispose the test engine.
    worker = create_worker(
        WorkerSettings,
        redis_settings=redis_settings(queue_settings),
        burst=True,
        poll_delay=0.01,
        max_burst_jobs=1,
        handle_signals=False,
        on_startup=None,
        on_shutdown=None,
    )
    completed = await worker.run_check(max_burst_jobs=1)
    assert completed == 1

    run = (await session.execute(select(SourceRun))).scalars().one()
    assert run.status == str(JobStatus.COMPLETED)
    assert run.items_created == 3

    refreshed = await reload_row(session, IngestionJob, job.id)
    assert refreshed.status == str(JobStatus.COMPLETED)
    assert refreshed.completed_at is not None
    assert refreshed.items_created == 3

    opportunities = (
        await session.execute(select(func.count()).select_from(ProcurementOpportunity))
    ).scalar_one()
    assert opportunities == 3


# --- failure handling -----------------------------------------------------


async def test_a_flaky_source_is_deferred_instead_of_dropped(
    queue_settings, session, source, worker_database, monkeypatch, mock_fetcher
):
    """A 503 from the source becomes a scheduled retry, not a lost job.

    Asserted on the job row as well as the exception: an operator reading
    ``ingestion_jobs`` must be able to tell "will try again at X" from "gave up".
    """
    from arq.worker import Retry

    monkeypatch.setattr(worker_tasks, "get_settings", lambda: queue_settings)

    # A 503 on the listing page: the fetcher exhausts its own retries and raises
    # RetryableFetchError, which the pipeline records instead of swallowing.
    fetcher = mock_fetcher({"https://example.org/tenders": (503, "bad gateway", "text/plain")})
    monkeypatch.setattr(worker_tasks, "HTTPFetcher", lambda *a, **k: fetcher)

    job = IngestionJob(
        source_id=source.id,
        job_type="SOURCE_INGEST",
        status=str(JobStatus.QUEUED),
        trigger=str(JobTrigger.SCHEDULER),
    )
    session.add(job)
    await session.commit()

    with pytest.raises(Retry):
        await worker_tasks.ingest_source({"job_try": 1}, str(source.id), job_id=str(job.id))

    stored = await reload_row(session, IngestionJob, job.id)
    assert stored.status == str(JobStatus.RETRYING)
    assert as_utc(stored.scheduled_for) > utcnow() - timedelta(seconds=5)
    assert "503" in (stored.error_message or "")

    errors = (
        (await session.execute(select(IngestionError).where(IngestionError.job_id == job.id)))
        .scalars()
        .all()
    )
    assert errors, "the failure must be recorded, not only logged"
    assert errors[0].retryable is True
    # The connector attributes this to the fetch stage, and the run is fatal
    # because discovery of the listing page never produced anything.
    assert errors[0].stage == "FETCH"


async def test_a_misconfigured_source_is_not_retried(
    queue_settings, session, source, worker_database, monkeypatch
):
    """An unknown connector is a fact about configuration: fail once, say why."""
    from arq.worker import JobExecutionFailed

    monkeypatch.setattr(worker_tasks, "get_settings", lambda: queue_settings)
    # No fetcher stub here: building the connector must fail first, which is the
    # point — a job that cannot even start must not open a socket.
    source.connector_key = "not.a.connector"
    job = IngestionJob(
        source_id=source.id,
        job_type="SOURCE_INGEST",
        status=str(JobStatus.QUEUED),
        trigger=str(JobTrigger.MANUAL),
    )
    session.add(job)
    await session.commit()

    with pytest.raises(JobExecutionFailed):
        await worker_tasks.ingest_source({"job_try": 1}, str(source.id), job_id=str(job.id))

    stored = await reload_row(session, IngestionJob, job.id)
    assert stored.status == str(JobStatus.FAILED)
    assert "connector" in (stored.error_message or "").lower()


async def test_retries_stop_after_the_configured_attempts(
    queue_settings, session, source, worker_database, monkeypatch, mock_fetcher
):
    """The last allowed attempt fails the job instead of deferring forever."""
    from arq.worker import JobExecutionFailed

    monkeypatch.setattr(worker_tasks, "get_settings", lambda: queue_settings)
    fetcher = mock_fetcher({"https://example.org/tenders": (503, "still down", "text/plain")})
    monkeypatch.setattr(worker_tasks, "HTTPFetcher", lambda *a, **k: fetcher)
    job = IngestionJob(
        source_id=source.id,
        job_type="SOURCE_INGEST",
        status=str(JobStatus.QUEUED),
        trigger=str(JobTrigger.SCHEDULER),
    )
    session.add(job)
    await session.commit()

    # worker_max_tries is 3 in these settings.
    with pytest.raises(JobExecutionFailed):
        await worker_tasks.ingest_source({"job_try": 3}, str(source.id), job_id=str(job.id))

    stored = await reload_row(session, IngestionJob, job.id)
    assert stored.status == str(JobStatus.FAILED)


# --- the cron-side tasks --------------------------------------------------


async def test_scheduler_offers_work_only_for_activated_unpaused_sources(
    queue_settings, session, source, queue, worker_database, monkeypatch
):
    from app.workers import queue as queue_module

    monkeypatch.setattr(queue_module, "_queue", queue)

    # A registered-but-unverified source and a paused one must both stay out of
    # the queue even though `active` is true for the first one.
    source.lifecycle_status = str(SourceLifecycle.DISCOVERED)
    unverified_ref = source.id

    from app.db.models.source import MunicipalitySource

    paused = MunicipalitySource(
        name="TEST FIXTURE paused source",
        slug="test-fixture-paused",
        organization="Test Fixture Municipality",
        source_type=source.source_type,
        base_url="https://example.org",
        procurement_scope=source.procurement_scope,
        municipality_id=source.municipality_id,
        province_id=source.province_id,
        connector_type=source.connector_type,
        connector_key="html.listing",
        lifecycle_status=str(SourceLifecycle.ACTIVE),
        paused_at=utcnow(),
        paused_reason="operator pause",
    )
    session.add(paused)
    await session.commit()

    result = await worker_tasks.schedule_due_sources({}, limit=25)
    assert result == {"queued": 0, "source_ids": []}
    assert await (await queue.connect()).zcard(QUEUE_NAME) == 0

    source.lifecycle_status = str(SourceLifecycle.ACTIVE)
    await session.commit()
    result = await worker_tasks.schedule_due_sources({}, limit=25)
    assert result["queued"] == 1
    assert str(unverified_ref) in result["source_ids"]

    # ARQ keeps the queue itself as a zset of job ids scored by due time.
    pool = await queue.connect()
    assert await pool.zcard(QUEUE_NAME) == 1
    stored = (
        (await session.execute(select(IngestionJob).where(IngestionJob.source_id == source.id)))
        .scalars()
        .one()
    )
    assert stored.status == str(JobStatus.QUEUED)
    assert stored.queue_job_id
    assert stored.trigger == str(JobTrigger.SCHEDULER)


async def test_document_task_reports_nothing_pending(session, worker_database, source):
    result = await worker_tasks.process_documents({}, limit=5)
    assert result == {"processed": 0, "failed": 0}


async def test_health_task_counts_unhealthy_sources_without_changing_them(
    session, source, worker_database
):
    source.consecutive_failures = 6
    source.health_status = str(HealthStatus.FAILING)
    await session.commit()

    before = (source.consecutive_failures, source.health_status, source.last_run_at)
    result = await worker_tasks.monitor_source_health({})
    assert result == {"unhealthy_sources": 1}
    await session.refresh(source)
    assert (source.consecutive_failures, source.health_status, source.last_run_at) == before
