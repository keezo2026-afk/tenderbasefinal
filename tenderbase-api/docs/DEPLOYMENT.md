# Deployment

The API and the worker run from the **same image** with different entrypoint roles.

```
                    ┌──────────┐        ┌──────────────┐
 clients ──HTTPS──▶ │  reverse │ ─────▶ │  api (N)     │ ──▶ PostgreSQL
                    │  proxy   │        └──────────────┘        ▲
                    └──────────┘        ┌──────────────┐        │
                                        │  worker (M)  │ ───────┘
                                        └──────┬───────┘
                                               ▼
                                          Redis + blob storage
```

## Build and run

```bash
docker build -f docker/Dockerfile -t tenderbase-api:0.1.0 .

docker run --rm -p 8000:8000 --env-file .env tenderbase-api:0.1.0 api
docker run --rm --env-file .env tenderbase-api:0.1.0 worker
docker run --rm --env-file .env tenderbase-api:0.1.0 migrate
```

Local full stack (PostgreSQL + Redis + API + worker):

```bash
docker compose -f docker/docker-compose.yml up --build
```

Image properties: `python:3.11-slim`, dependencies installed into `/opt/venv` in a builder stage,
runs as the non-root user `tenderbase` (uid 10001), `HEALTHCHECK` on `/api/v1/health/live`,
no secrets baked in.

## Configuration

Everything comes from the environment. Required in production:

| Variable | Notes |
| --- | --- |
| `APP_ENV=production` | Rejects `DEBUG=true` and the placeholder secret at startup |
| `SECRET_KEY` | `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `DATABASE_URL` | `postgresql+psycopg://user:pass@host:5432/tenderbase` |
| `REDIS_URL` | Workers; also the distributed rate-limit backend |
| `CORS_ORIGINS` | Explicit list — do not ship `*` (empty by default: there is no browser client) |
| `LOG_JSON=true` | Structured logs for aggregation |
| `HTTP_USER_AGENT` | Must identify the crawler and a contact address |
| `API_KEY_PEPPER` | Set once, keep stable. Rotating it invalidates every issued API key |
| `API_KEY_SELF_SERVICE_ENABLED=false` | Leave false: mint keys with `scripts/manage_api_keys.py` so issuance is an audited action |
| `METRICS_TOKEN` | Required if `/metrics` is reachable from anything but a private network |
| `DOCUMENT_STORAGE_BACKEND` | `local` (default) or `s3`; `s3` additionally requires `S3_BUCKET` and should set `S3_REGION` |
| `SOURCE_CLAIM_LEASE_SECONDS` | 1800. Startup refuses a lease shorter than `WORKER_JOB_TIMEOUT_SECONDS`, or a run would outlive its lease and a second worker could start the same source |
| `JOB_RECONCILIATION_INTERVAL_SECONDS` | 300 — how often the reconcile job sweeps. Floor 30 s; a period ARQ cron cannot express is rounded **up** to the next tick and the worker logs `worker.reconciliation_interval_coarsened` |
| `JOB_QUEUED_STALE_AFTER_SECONDS` / `JOB_RUNNING_GRACE_SECONDS` | How long before the reconciler calls a job lost (enforced floors: 60 s / 120 s, the latter added to `WORKER_JOB_TIMEOUT_SECONDS`). Raise the grace if queue latency under load is longer than 5 minutes |
| `RECONCILE_REENQUEUE` | `true` re-dispatches a stale job that still has retries left; `false` fails it and lets the next scheduled tick claim the source normally |
| `FRESHNESS_AGING_HOURS` / `FRESHNESS_STALE_HOURS` | 36 / 96 — the `fresh`/`aging`/`stale` buckets behind `/operations/sources/freshness` and `tenderbase_source_freshness_hours` |
| `RATE_LIMIT_ENABLED` / `RATE_LIMIT_FAIL_OPEN` | See the rate-limit note below |

Keep `HTTP_ALLOW_PRIVATE_NETWORKS=false` in every deployed environment. The outbound **port**
allowlist is `HTTP_ALLOWED_PORTS=80,443,8080,8443`; a source that needs another port is a deliberate
configuration change with a recorded reason, not a widening of the default for everyone
(`docs/SECURITY.md` explains why the port check exists and runs before the address check).

Rate limiting is Redis-backed with a per-process fallback. `RATE_LIMIT_FAIL_OPEN=true` (the default)
keeps serving during a Redis outage with per-replica limits — visible as
`X-RateLimit-Policy: in-process (redis unavailable)`, and reported by `/health` as `degraded` without
blocking traffic. `RATE_LIMIT_FAIL_OPEN=false` answers 503 on protected endpoints and makes readiness
fail, which is the correct choice only if exceeding a limit costs more than read availability.

## Migrations

Run migrations as a separate step in the release pipeline:

```bash
docker run --rm --env-file .env tenderbase-api:0.1.0 migrate
```

`RUN_MIGRATIONS_ON_START=true` is available for single-instance/dev deployments only — with several
API replicas it causes concurrent `alembic upgrade` runs.

First-time setup after migrating:

```bash
python -m scripts.import_geography --provinces
python -m scripts.seed_categories
python -m scripts.sync_connectors
python -m scripts.import_sources /path/to/verified-sources.json
```

## Scaling

| Component | Notes |
| --- | --- |
| API | Stateless; scale horizontally. `UVICORN_WORKERS` per container (default 2), sized to CPU |
| Worker | Scale by number of sources; ARQ jobs are per-source, so concurrency is naturally bounded |
| PostgreSQL | The read workload is index-friendly; the FTS/trigram indexes from `b2f1c9d40a11` matter once the dataset grows |
| Redis | Queue only; no application state |
| Blob storage | Local volume works for one host; `DOCUMENT_STORAGE_BACKEND=s3` (S3, MinIO, R2, Ceph) is the multi-host option — see `docs/DOCUMENTS.md` |

Crawling is deliberately polite: per-host rate limits, robots.txt, backoff on failure. Do not scale
workers to "go faster" against a single municipal site.

## Observability

* **Logs** — structlog; set `LOG_JSON=true`. Every line carries `request_id`, plus `source_id` and
  `job_id` inside ingestion. Secrets are redacted by a processor. Both properties hold for *every*
  module because logging is configured when `app.logging` is imported and the destination, level and
  renderer are resolved per write rather than captured at import: an application that only configured
  logging inside `create_app()` would emit early-imported modules' lines in console format on stdout,
  unredacted, which is precisely what a log pipeline must not do. Services write to **stdout** (what
  the runtime collects); the CLI scripts in `scripts/` write to **stderr**, because their stdout is
  the result an operator pipes into `jq`.
* **Request tracing** — `X-Request-ID` is accepted from the proxy and echoed on every response,
  including errors.
* **Health** — `/api/v1/health/live` for liveness (no I/O, so it cannot flap and restart a pod that is
  fine), `/api/v1/health/ready` for readiness (database always; Redis only when the limiter may not
  degrade), `/api/v1/health` for the full component list with latencies. The same three are mounted at
  the root (`/health/ready` …) for probes that must not know the API version — the image's
  `HEALTHCHECK` uses one. Every probe is bounded to 1.5 s per dependency and never raises: a probe that
  can hang is worse than one that reports a failure.
* **Metrics** — `/metrics` (Prometheus text, excluded from the OpenAPI schema, optionally
  `METRICS_TOKEN`-protected). Counters/histograms for HTTP by route and status class, ingestion runs
  and dedup decisions, document extraction, auth rejections and rate-limit decisions, plus gauges
  sampled at scrape time: queue depth, per-health failure maxima, and
  `tenderbase_db_pool_connections{state=...}` — `checked_out` sitting at `size` is the first sign the
  pool is too small, and it shows up as latency long before it shows up as an error.
* **Scraping and the anonymous limiter** — `/metrics` is public by design, so it is charged to the
  anonymous rate-limit tier (`RATE_LIMIT_ANONYMOUS_PER_MINUTE`, default 20/min plus a burst of
  `RATE_LIMIT_BURST`) per client IP. A 15 s scrape interval is 4 requests/min and never competes for
  that budget, but a 1 s interval, or several Prometheus servers scraping one replica, will start
  receiving 429s — raise the tier or widen the window rather than pointing scrapers at a
  rate-limit-free path, since an unbounded public endpoint is the thing being protected. Set
  `METRICS_TOKEN` and the payload stops being public; the token does **not** exempt the scrape from the
  limit, because exemption would be a way to bypass the limiter, not a way to authenticate to it.
* **Operational data** — `source_runs`, `ingestion_jobs` and `ingestion_errors` are queryable; the API
  surfaces source health and run history at `/api/v1/sources/{id}/runs`, and the full run report, failed
  runs across the fleet, unhealthy sources and the duplicate review queue under `/api/v1/operations/*`
  (scope `read:sources`). Or from the shell: `python -m scripts.run_report`.
* **Post-deploy** — `python -m scripts.sync_connectors --dry-run` should report nothing to update (a
  diff means the registry table is describing a connector that no longer exists, or is stale about
  whether it is production-ready); `alembic upgrade head` must be a no-op.
* **Statistics** — `/api/v1/statistics` is cached in-process (`STATISTICS_CACHE_SECONDS`).

Suggested alerts: sources in `FAILING`/`OFFLINE`, a rising `ingestion_errors` rate, zero successful
runs in 24 h, readiness failures, p95 latency on `/tenders` and `/search`, and
`increase(tenderbase_rate_limit_total{outcome="block"}[15m])` summed by `backend` — blocks recorded
against `backend="in-process (redis unavailable)"` mean limits are being enforced per replica during a
Redis incident. `tenderbase_auth_rejections_total{code="API_KEY_INVALID"}` spiking from one IP is
someone probing for keys, which is worth a page precisely because the API answers identically either way.

## Reverse proxy

Terminate TLS at the proxy, forward `X-Forwarded-For`/`X-Forwarded-Proto` (the entrypoint enables
`--proxy-headers`), and set request-body and timeout limits there. The API sets no cookies and
holds no session state.

## Scheduling, claims and reconciliation

Every API replica and the worker run `schedule_due_sources` on a cron tick. Before it enqueues
anything, that job claims due sources in the database: an ids-first `SELECT … FOR UPDATE SKIP LOCKED`
over the rows that are active, unsuppressed, due and unclaimed, then the claim (`next_run_at`,
`claim_expires_at`, `claim_job_id`) and the `ingestion_jobs` row committed together. Two schedulers
therefore receive disjoint sets, a source that is already claimed is invisible to the next tick rather
than enqueued-and-rejected, and a crashed worker's source becomes claimable again when its lease
expires. Releasing a claim and rescheduling the next window happen in the ingestion job's own
transaction, so the two can never disagree.

`reconcile_jobs` runs every `JOB_RECONCILIATION_INTERVAL_SECONDS` and repairs what a lost worker, a
dropped queue or a failed `finally` block leaves behind: a `SourceRun` still `RUNNING` under a job that
already reached a terminal state is closed `FAILED` with its counters and duration preserved; a live
job that stopped moving is re-enqueued (or failed, with `RECONCILE_REENQUEUE=false`), its source's
failure counters advanced so the normal backoff applies and its next run is made due now; an expired
lease is cleared; a duplicate live claim is cancelled. It reuses the claim query's own predicate, so a
source another worker is currently holding is never touched, and it never breaks a live lease or
resurrects a terminally failed job.

The same repair is an operator endpoint: `POST /api/v1/operations/reconcile` (admin scope, `?dry_run=true`
to see what would change, repeatable — a second pass reports zero actions). Each action is counted in
`tenderbase_recovery_actions_total`, and `GET /api/v1/operations/sources/freshness` answers "is
ingestion actually delivering, or just running?" without a psql session: active-and-claimable sources
first, worst first, with `PAUSED` / `NOT_ACTIVE` reported rather than silently dropped.

## Backups

* PostgreSQL is the system of record — nightly `pg_dump` plus WAL archiving.
* Blob storage holds retrievable-in-principle documents, but source sites remove files, so back it
  up too if document history matters.
* Restore drill: restore the database, run `alembic upgrade head`, start the API, check
  `/api/v1/health/ready` and `/api/v1/statistics`.

## Deployment checklist

1. `SECRET_KEY` set (and `API_KEY_PEPPER` if you want issued keys to survive a `SECRET_KEY` rotation),
   `DEBUG=false`, `APP_ENV=production`
2. `CORS_ORIGINS` restricted; `HTTP_USER_AGENT` identifies you with a contact address
3. Migrations applied as a discrete step
4. Geography, categories and connectors seeded; sources imported from **verified** definitions, each one
   passed through `python -m scripts.verify_source <id>` before it is activated
5. API keys issued for every consumer (`scripts/manage_api_keys.py`), with the narrowest scope that
   works; `API_KEY_SELF_SERVICE_ENABLED` left false
6. `RATE_LIMIT_ENABLED=true` with a Redis instance the API can reach, or an accepted decision to run
   per-replica limits; `METRICS_TOKEN` set if `/metrics` is not on a private network
7. Health checks wired into the orchestrator (readiness for traffic, liveness for restarts); log
   aggregation receiving JSON logs
8. Backups configured and a restore tested
9. `GET /api/v1/health/ready` returns 200, `GET /metrics` renders, and an anonymous
   `GET /api/v1/tenders` returns 401 — three checks that confirm the deployment is what you think it is
