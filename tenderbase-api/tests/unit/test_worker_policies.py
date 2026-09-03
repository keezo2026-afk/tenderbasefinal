"""Unit tests for the worker-level retry policy and queue plumbing.

Nothing here needs Redis or a database: the point of these tests is the decision
logic (retry or give up, and for how long), which must be verifiable without a
broker. The queue's connection handling is covered against a fake pool.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.enums import ErrorStage
from app.errors import ServiceUnavailableError
from app.ingestion.parser import StageError
from app.utils.backoff import exponential_backoff_seconds
from app.utils.dates import utcnow

MAX = 900.0


def run_with(*errors: StageError) -> SimpleNamespace:
    stats = {"error_count": len(errors), "errors": [e.as_dict() for e in errors]}
    return SimpleNamespace(id="run-1", source_id="src-1", stats=stats, error_message=None)


FETCH_FAILED = StageError(
    stage=ErrorStage.FETCH, code="FETCH_RETRYABLE", message="502 bad gateway", retryable=True
)
CONFIG_BROKEN = StageError(
    stage=ErrorStage.DISCOVERY,
    code="CONNECTOR_NOT_REGISTERED",
    message="no such connector",
    retryable=False,
)


# --- backoff --------------------------------------------------------------


def test_backoff_grows_exponentially_before_jitter():
    """Deterministic mode exposes the curve the jitter is applied to."""
    delays = [
        exponential_backoff_seconds(i, base_seconds=5.0, max_seconds=MAX, jitter=False)
        for i in range(5)
    ]
    assert delays == pytest.approx([5.0, 10.0, 20.0, 40.0, 80.0])


def test_backoff_is_capped():
    value = exponential_backoff_seconds(30, base_seconds=5.0, max_seconds=60.0, jitter=False)
    assert value == 60.0


def test_full_jitter_stays_within_the_exponential_envelope():
    """Jitter must never make a wait *longer* than the curve, only shorter."""
    for attempt in range(6):
        ceiling = min(MAX, 2.0 * (2**attempt))
        drawn = [
            exponential_backoff_seconds(attempt, base_seconds=2.0, max_seconds=MAX)
            for _ in range(200)
        ]
        assert all(0.0 <= value <= ceiling for value in drawn)
        # A spread of draws is the whole point: they must not all be equal.
        assert len({round(v, 6) for v in drawn}) > 1


def test_backoff_rejects_nonsense():
    with pytest.raises(ValueError):
        exponential_backoff_seconds(-1, base_seconds=1.0)
    with pytest.raises(ValueError):
        exponential_backoff_seconds(0, base_seconds=-1.0)


# --- retry decisions ------------------------------------------------------


def test_transient_failure_is_deferred_with_backoff():
    from app.workers.retry import decide_retry

    settings = Settings(app_env="test", worker_max_tries=4, worker_retry_backoff_seconds=7.0)
    decision = decide_retry(run_with(FETCH_FAILED), job_try=2, settings=settings)
    assert decision.retry is True
    assert decision.reason == "transient_failure"
    # Attempt 2 of 4 -> the second slot of the curve, before jitter: 14s.
    assert 0.0 <= decision.delay_seconds <= 14.0
    assert decision.delay >= timedelta(0)


def test_permanent_failure_is_not_retried():
    from app.workers.retry import decide_retry

    decision = decide_retry(
        run_with(CONFIG_BROKEN),
        job_try=1,
        settings=Settings(app_env="test", worker_max_tries=5),
    )
    assert decision.retry is False
    assert decision.reason == "permanent_failure"
    assert decision.delay_seconds == 0.0


def test_a_mixed_run_still_gets_one_more_try():
    """One retryable error among permanent ones justifies another attempt.

    A source whose listing page timed out but whose two detail pages were
    malformed is worth re-fetching; the malformed items stay failed either way.
    """
    from app.workers.retry import decide_retry

    decision = decide_retry(
        run_with(CONFIG_BROKEN, FETCH_FAILED),
        job_try=1,
        settings=Settings(app_env="test", worker_max_tries=3),
    )
    assert decision.retry is True


def test_retries_stop_at_the_configured_maximum():
    from app.workers.retry import decide_retry

    settings = Settings(app_env="test", worker_max_tries=3, worker_retry_backoff_seconds=1.0)
    exhausted = decide_retry(run_with(FETCH_FAILED), job_try=3, settings=settings)
    assert exhausted.retry is False
    assert exhausted.reason == "retries_exhausted"
    assert exhausted.attempts_used == 3


def test_job_try_below_one_is_treated_as_a_first_attempt():
    """A task called from a script has no ARQ context."""
    from app.workers.retry import decide_retry

    settings = Settings(app_env="test", worker_max_tries=3, worker_retry_backoff_seconds=2.0)
    for job_try in (0, -5, None):
        decision = decide_retry(
            run_with(FETCH_FAILED),
            job_try=job_try if job_try is not None else 1,
            settings=settings,
        )
        assert decision.attempts_used == 1
        assert decision.retry is True


def test_a_failure_with_no_recorded_error_still_gets_one_try():
    from app.workers.retry import decide_retry

    run = SimpleNamespace(id="r", source_id="s", stats=None, error_message="upstream said no")
    decision = decide_retry(run, job_try=1, settings=Settings(app_env="test", worker_max_tries=2))
    assert decision.retry is True


def test_retry_delay_never_looks_like_a_reschedule():
    """Deferred retries are bounded, whatever WORKER_RETRY_BACKOFF_SECONDS says."""
    from app.workers.retry import MAX_RETRY_DELAY_SECONDS, decide_retry

    settings = Settings(
        app_env="test", worker_max_tries=10, worker_retry_backoff_seconds=10_000_000.0
    )
    decision = decide_retry(run_with(FETCH_FAILED), job_try=6, settings=settings)
    assert decision.delay_seconds <= MAX_RETRY_DELAY_SECONDS


# --- the control-flow half ------------------------------------------------


async def test_defer_or_fail_records_the_job_and_raises_retry(monkeypatch):
    from arq.worker import Retry

    from app.workers.retry import defer_or_fail

    job = SimpleNamespace(
        id="job-1", status="RUNNING", scheduled_for=None, completed_at=None, error_message=None
    )
    committed = []

    class Session:
        async def commit(self):
            committed.append(True)

    with pytest.raises(Retry):
        await defer_or_fail(
            {"job_try": 1},
            run=run_with(FETCH_FAILED),
            job=job,
            settings=Settings(app_env="test", worker_max_tries=3),
            session=Session(),
        )
    assert job.status == "RETRYING"
    assert job.scheduled_for is not None
    assert job.completed_at is None
    assert "502 bad gateway" in job.error_message
    # Committed before raising: the surrounding scope rolls back on the way out.
    assert committed == [True]


async def test_defer_or_fail_gives_up_without_committing_a_retry(monkeypatch):
    from arq.worker import JobExecutionFailed

    from app.workers.retry import defer_or_fail

    job = SimpleNamespace(
        id="job-1", status="RUNNING", scheduled_for=None, completed_at=None, error_message=None
    )
    with pytest.raises(JobExecutionFailed):
        await defer_or_fail(
            {"job_try": 5},
            run=run_with(FETCH_FAILED),
            job=job,
            settings=Settings(app_env="test", worker_max_tries=5),
            session=None,
        )
    assert job.status == "FAILED"
    assert job.completed_at is not None


# --- worker configuration -------------------------------------------------


def test_worker_settings_are_taken_from_configuration(monkeypatch):
    """No hard-coded duplicates of settings in the worker definition."""
    import importlib

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WORKER_MAX_JOBS", "3")
    monkeypatch.setenv("WORKER_JOB_TIMEOUT_SECONDS", "123")
    monkeypatch.setenv("WORKER_MAX_TRIES", "2")
    monkeypatch.setenv("WORKER_KEEP_RESULT_SECONDS", "55")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6399/4")

    import app.config as config_module
    import app.workers.scheduler as scheduler_module

    # get_settings() is cached; without clearing it the reload would pick up the
    # process-wide settings and prove nothing about the environment above.
    config_module.get_settings.cache_clear()
    monkeypatch.setattr(config_module, "settings", config_module.Settings())
    reloaded = importlib.reload(scheduler_module)
    try:
        assert reloaded.WorkerSettings.max_jobs == 3
        assert reloaded.WorkerSettings.job_timeout == 123
        assert reloaded.WorkerSettings.max_tries == 2
        assert reloaded.WorkerSettings.keep_result == 55
        assert reloaded.WorkerSettings.redis_settings.database == 4
        assert reloaded.WorkerSettings.queue_name == "tenderbase:ingestion"
        # ARQ wraps plain coroutine functions itself, so the class holds the
        # functions, not arq.Function objects.
        names = [f.__name__ for f in reloaded.WorkerSettings.functions]
        assert names == [
            "ingest_source",
            "schedule_due_sources",
            "process_documents",
            "monitor_source_health",
        ]
        assert len(reloaded.WorkerSettings.cron_jobs) == 3
    finally:
        importlib.reload(scheduler_module)
        config_module.get_settings.cache_clear()


def test_redis_url_mistakes_are_reported_by_name():
    from app.workers.queue import redis_settings

    for bad, fragment in [
        ("", "REDIS_URL is empty"),
        ("localhost:6379", "scheme"),
    ]:
        with pytest.raises(ValueError, match=fragment):
            redis_settings(Settings(app_env="test", redis_url=bad))


# --- queue plumbing -------------------------------------------------------


async def test_enqueue_surfaces_a_clear_error_when_redis_is_down(monkeypatch):
    import arq

    from app.workers.queue import JobQueue

    async def boom(*args, **kwargs):
        raise ConnectionError("Connection refused")

    monkeypatch.setattr(arq, "create_pool", boom)
    queue = JobQueue(Settings(app_env="test", redis_url="redis://127.0.0.1:1/0"))
    with pytest.raises(ServiceUnavailableError) as excinfo:
        await queue.enqueue("ingest_source", "some-id")
    assert excinfo.value.code == "QUEUE_UNAVAILABLE"
    # The message names the configured URL so the operator knows what to fix.
    assert "127.0.0.1:1" in str(excinfo.value)
    assert await queue.health() is False


async def test_duplicate_job_ids_are_ignored_rather_than_double_queued(monkeypatch):
    """ARQ returns ``None`` for an id it has already seen — not an error."""
    import arq

    from app.workers.queue import JobQueue

    calls = []

    class Pool:
        async def enqueue_job(self, task, *args, **kwargs):
            calls.append((task, args, kwargs))
            return None

        async def close(self):
            pass

    async def create_pool(*args, **kwargs):
        return Pool()

    monkeypatch.setattr(arq, "create_pool", create_pool)
    queue = JobQueue(Settings(app_env="test", redis_url="redis://127.0.0.1:6379/0"))
    assert await queue.enqueue("ingest_source", "id-1", unique_id="job-1") is None
    task, args, kwargs = calls[0]
    assert (task, args) == ("ingest_source", ("id-1",))
    assert kwargs["_job_id"] == "job-1"
    assert kwargs["_queue_name"] == "tenderbase:ingestion"


async def test_task_kwargs_survive_the_queue_options(monkeypatch):
    """Regression: ``job_id`` belongs to the *task*, not to the queue.

    Naming the queue's dedupe key ``job_id`` would swallow the ingestion job id
    that ``ingest_source`` needs, and the worker would quietly create a second
    ``ingestion_jobs`` row instead of updating the one it was given.
    """
    import arq

    from app.workers.queue import JobQueue

    captured = {}

    class Pool:
        async def enqueue_job(self, task, *args, **kwargs):
            captured.update({"task": task, "args": args, "kwargs": kwargs})

            class Job:
                job_id = "auto-1"

            return Job()

        async def close(self):
            pass

    async def create_pool(*args, **kwargs):
        return Pool()

    monkeypatch.setattr(arq, "create_pool", create_pool)
    queue = JobQueue(Settings(app_env="test", redis_url="redis://127.0.0.1:6379/0"))
    assert await queue.enqueue("ingest_source", "src-1", job_id="our-job-1") == "auto-1"
    assert captured["kwargs"]["job_id"] == "our-job-1"
    assert "_job_id" not in captured["kwargs"]


async def test_deferred_enqueue_uses_arq_defer_option(monkeypatch):
    import arq

    from app.workers.queue import JobQueue

    captured = {}

    class Pool:
        async def enqueue_job(self, task, *args, **kwargs):
            captured.update(kwargs)

            class Job:
                job_id = "job-9"

            return Job()

        async def close(self):
            pass

    async def create_pool(*args, **kwargs):
        return Pool()

    monkeypatch.setattr(arq, "create_pool", create_pool)
    queue = JobQueue(Settings(app_env="test", redis_url="redis://127.0.0.1:6379/0"))
    assert await queue.enqueue("process_documents", defer_seconds=45) == "job-9"
    assert captured["_defer_by"] == timedelta(seconds=45)


async def test_defer_options_are_mutually_exclusive():
    """ARQ rejects "defer until" plus "defer by"; say so before the round trip."""
    from app.workers.queue import JobQueue

    queue = JobQueue(Settings(app_env="test", redis_url="redis://127.0.0.1:6379/0"))
    with pytest.raises(ValueError, match="not both"):
        await queue.enqueue("x", defer_seconds=5, defer_until=utcnow())
