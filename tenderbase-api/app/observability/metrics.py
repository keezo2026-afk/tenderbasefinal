"""Prometheus metrics: HTTP instrumentation plus domain counters.

Two groups live here deliberately:

1. **Request metrics** come from ``prometheus-fastapi-instrumentator`` so the
   buckets, naming and ``/metrics`` rendering are the ecosystem-standard ones
   rather than a bespoke re-invention.
2. **Domain metrics** (ingestion, documents, deduplication, queue) are declared
   explicitly below because they are what actually distinguishes operating
   TenderBase from operating a generic FastAPI service: they are the counters an
   engineer checks at 03:00 when a source stops producing data.

Exposure: ``GET /metrics`` is **not** part of the public OpenAPI schema. Bind it
to an internal interface behind a reverse proxy, or protect it with
``METRICS_TOKEN`` when the port is reachable from anywhere else. No metric label
may contain a URL, client IP, request id or API-key material — labels are bounded
enums only, because unbounded labels are a memory-exhaustion DoS vector.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from prometheus_client import REGISTRY, Counter, Gauge, Histogram, Info

from app.config import Settings

# --- Ingestion ------------------------------------------------------------

INGESTION_JOBS = Counter(
    "tenderbase_ingestion_jobs_total",
    "Ingestion jobs by outcome and terminal state.",
    ["status"],
)
INGESTION_ITEMS = Counter(
    "tenderbase_ingestion_items_total",
    "Records handled by the pipeline, by outcome.",
    ["outcome"],  # created | updated | skipped | failed
)
INGESTION_SOURCES = Counter(
    "tenderbase_ingestion_source_runs_total",
    "Per-source pipeline runs (health transitions and operator dashboards).",
    ["outcome"],  # success | failure
)
INGESTION_DURATION = Histogram(
    "tenderbase_ingestion_run_seconds",
    "Wall-clock duration of one source run.",
    buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 900),
)
INGESTION_DUPLICATES = Counter(
    "tenderbase_ingestion_duplicates_total",
    "Deduplication decisions, by layer and decision.",
    ["layer", "decision"],
)
SOURCE_CONSECUTIVE_FAILURES = Gauge(
    "tenderbase_source_consecutive_failures",
    "Consecutive failures recorded for a source (updated at end of each run).",
    ["health_status"],
)
QUEUE_DEPTH = Gauge(
    "tenderbase_queue_depth",
    "Queued ingestion jobs waiting for a worker (Redis LLEN of the ARQ queue).",
)
QUEUE_WORKERS = Gauge(
    "tenderbase_queue_running_jobs",
    "Ingestion jobs currently executing (ARQ running-set cardinality).",
)
DB_POOL_CONNECTIONS = Gauge(
    "tenderbase_db_pool_connections",
    "Database pool connections by state, sampled at scrape time. "
    '``state="checked_out"`` at ``state="size"`` means requests are queueing '
    "for a connection — the usual first symptom of a pool that is too small for "
    "the API's concurrency.",
    ["state"],
)

# --- Documents ------------------------------------------------------------

DOCUMENTS_PROCESSED = Counter(
    "tenderbase_documents_total",
    "Document processing outcomes.",
    ["outcome", "method"],  # outcome: success|failure, method: NATIVE_PDF|OCR|...
)
DOCUMENT_EXTRACTION_DURATION = Histogram(
    "tenderbase_document_extraction_seconds",
    "Text extraction duration per document.",
    ["method"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 120),
)
DOCUMENT_BYTES = Histogram(
    "tenderbase_document_bytes",
    "Size of processed documents in bytes.",
    buckets=(10_000, 100_000, 500_000, 1_000_000, 5_000_000, 25_000_000, 100_000_000),
)

# --- Data quality ----------------------------------------------------------
# These extend the *label values* of the counters above (``INGESTION_ITEMS`` gains
# ``found|unchanged|invalid|needs_review|duplicates|fuzzy_duplicates``,
# ``DOCUMENTS_PROCESSED`` gains ``found|downloaded|failed``) rather than introducing
# parallel counters for the same facts. Only genuinely new subjects get a metric here.

OCR_REQUIRED = Counter(
    "tenderbase_ingestion_ocr_required_total",
    "Documents where native text extraction produced too little and OCR was required.",
    ["outcome"],  # required | performed | unavailable | skipped
)
SOURCE_FRESHNESS_HOURS = Histogram(
    "tenderbase_source_freshness_hours",
    "Hours since each source's last successful run, sampled by the reconciliation pass.",
    buckets=(0.5, 1, 3, 6, 12, 24, 36, 48, 72, 96, 168, 336, 720),
)
SOURCE_FRESHNESS = Gauge(
    "tenderbase_source_freshness_records",
    "Active sources by freshness classification (FRESH/AGING/STALE/NEVER_RUN/DISABLED).",
    ["state"],
)
SCHEDULE_CLAIMS = Counter(
    "tenderbase_schedule_claims_total",
    "Source claims taken by the scheduler. ``contended`` means every due source was "
    "already leased by another worker — normal once a second, a sign of overlap if it "
    "dominates.",
    ["outcome"],  # claimed | contended
)
RECOVERY_ACTIONS = Counter(
    "tenderbase_recovery_actions_total",
    "Repairs applied by the reconciliation pass, by action.",
    ["action"],
)

# --- Worker / queue health ------------------------------------------------

WORKER_JOBS = Counter(
    "tenderbase_worker_jobs_total",
    "Worker job executions by task and outcome.",
    ["task", "outcome"],  # success | retry | failure
)

# --- API security ---------------------------------------------------------

AUTH_REJECTIONS = Counter(
    "tenderbase_auth_rejections_total",
    "Rejected API authentications by reason code. Never contains key material.",
    ["code"],
)
RATE_LIMIT_DECISIONS = Counter(
    "tenderbase_rate_limit_total",
    "Rate limiter decisions by tier and outcome.",
    ["tier", "outcome", "backend"],
)
RATE_LIMIT_REJECTS = Counter(
    "tenderbase_rate_limit_rejects_total",
    "Requests rejected with 429.",
    ["tier"],
)

# --- Data volumes (cheap gauges refreshed by the scheduler) ---------------

TENDERS_TOTAL = Gauge("tenderbase_tenders_total", "Canonical opportunities stored.")
TENDERS_CREATED = Counter("tenderbase_tenders_created_total", "Opportunities created by ingestion.")
TENDERS_UPDATED = Counter("tenderbase_tenders_updated_total", "Opportunities updated by ingestion.")
UNCERTAIN_MATCHES = Counter(
    "tenderbase_uncertain_matches_total",
    "Deduplication results held for human review (never auto-merged).",
)

BUILD_INFO = Info("tenderbase_build", "Build/environment information (non-secret labels only).")

# --- HTTP -----------------------------------------------------------------
# Declared here rather than via prometheus-fastapi-instrumentator because that
# library registers its collectors in the *global* registry at app-build time:
# a test suite (or an app factory called more than once) then dies on
# DuplicateTimeseries. Owning the collectors also lets us label by the matched
# **route template**, which is bounded, instead of by raw path, which is not.
HTTP_REQUESTS = Counter(
    "tenderbase_http_requests_total",
    "HTTP requests by method, route template and status class.",
    ["method", "route", "status_class"],
)
HTTP_LATENCY = Histogram(
    "tenderbase_http_request_duration_seconds",
    "HTTP request latency by method and route template.",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
HTTP_INPROGRESS = Gauge(
    "tenderbase_http_requests_inprogress",
    "Requests currently being served.",
    ["method"],
)


def observe_http_request(
    *, method: str, route: str, status_code: int, duration_seconds: float
) -> None:
    """Record one request. ``route`` must be a template, never a raw path."""
    status_class = f"{status_code // 100}xx"
    HTTP_REQUESTS.labels(method=method, route=route, status_class=status_class).inc()
    HTTP_LATENCY.labels(method=method, route=route).observe(duration_seconds)


def route_label(request: Any) -> str:
    """Bounded identifier for the matched route.

    Falls back to ``"unmatched"`` for 404s, so a scan for random URLs cannot
    create an unbounded number of label values (a real DoS vector in
    Prometheus when paths are used as labels).
    """
    route = getattr(request, "scope", {}).get("route") if hasattr(request, "scope") else None
    path = getattr(route, "path", None)
    if path:
        return str(path)
    return "unmatched"


def set_build_info(settings: Settings) -> None:
    """Publish version/environment as an ``INFO`` metric for dashboards."""
    BUILD_INFO.info(
        {
            "version": settings.app_version,
            "environment": settings.app_env,
            "search_backend": "postgres",
            "ai_enabled": str(settings.ai_enabled).lower(),
        }
    )


def observe_run(
    *,
    outcome: str,
    duration_seconds: float,
    created: int = 0,
    updated: int = 0,
    skipped: int = 0,
    failed: int = 0,
    counts: Mapping[str, int] | None = None,
) -> None:
    """Record the result of one source run (called from the pipeline and workers).

    ``counts`` carries the run's remaining per-record outcomes (``found``,
    ``unchanged``, ``invalid``, ``needs_review``, ``duplicates``, ...) and is added to
    the same ``INGESTION_ITEMS`` counter, so a new statistic is a new label value
    rather than a new metric family.
    """
    INGESTION_SOURCES.labels(outcome=outcome).inc()
    INGESTION_DURATION.observe(duration_seconds)
    INGESTION_JOBS.labels(status=outcome).inc()
    for name, count in (
        ("created", created),
        ("updated", updated),
        ("skipped", skipped),
        ("failed", failed),
        *(counts or {}).items(),
    ):
        if count:
            INGESTION_ITEMS.labels(outcome=name).inc(count)
    if created:
        TENDERS_CREATED.inc(created)
    if updated:
        TENDERS_UPDATED.inc(updated)


def observe_dedup(layer: str, decision: str, *, uncertain: int = 0) -> None:
    INGESTION_DUPLICATES.labels(layer=layer, decision=decision).inc()
    if uncertain:
        UNCERTAIN_MATCHES.inc(uncertain)


def snapshot_gauges(values: dict[str, Any]) -> None:
    """Refresh the small set of gauge-style facts exposed by the API."""
    if "tenders_total" in values:
        TENDERS_TOTAL.set(float(values["tenders_total"]))
    if "queue_depth" in values:
        QUEUE_DEPTH.set(float(values["queue_depth"]))
    if "queue_running" in values:
        QUEUE_WORKERS.set(float(values["queue_running"]))
    for state, count in (values.get("source_freshness") or {}).items():
        SOURCE_FRESHNESS.labels(state=str(state)).set(float(count))
    for status, count in (values.get("source_failures_by_health") or {}).items():
        SOURCE_CONSECUTIVE_FAILURES.labels(health_status=str(status)).set(float(count))
    for state, count in (values.get("db_pool") or {}).items():
        DB_POOL_CONNECTIONS.labels(state=str(state)).set(float(count))


def render_metrics() -> bytes:
    """Expose Prometheus text format for the (protected) ``/metrics`` endpoint."""
    from prometheus_client import generate_latest

    return generate_latest(REGISTRY)


__all__ = [
    "AUTH_REJECTIONS",
    "DB_POOL_CONNECTIONS",
    "DOCUMENTS_PROCESSED",
    "DOCUMENT_BYTES",
    "DOCUMENT_EXTRACTION_DURATION",
    "INGESTION_DURATION",
    "INGESTION_DUPLICATES",
    "INGESTION_ITEMS",
    "INGESTION_JOBS",
    "INGESTION_SOURCES",
    "QUEUE_DEPTH",
    "QUEUE_WORKERS",
    "RATE_LIMIT_DECISIONS",
    "RATE_LIMIT_REJECTS",
    "SOURCE_CONSECUTIVE_FAILURES",
    "TENDERS_CREATED",
    "TENDERS_TOTAL",
    "TENDERS_UPDATED",
    "UNCERTAIN_MATCHES",
    "WORKER_JOBS",
    "observe_dedup",
    "observe_run",
    "render_metrics",
    "snapshot_gauges",
]
