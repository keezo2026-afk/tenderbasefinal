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
| `REDIS_URL` | Workers |
| `CORS_ORIGINS` | Explicit list — do not ship `*` |
| `LOG_JSON=true` | Structured logs for aggregation |
| `HTTP_USER_AGENT` | Must identify the crawler and a contact address |

Keep `HTTP_ALLOW_PRIVATE_NETWORKS=false` in every deployed environment.

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
| Blob storage | Local volume works for one host; use an S3-compatible backend behind `BlobStorage` for multi-host |

Crawling is deliberately polite: per-host rate limits, robots.txt, backoff on failure. Do not scale
workers to "go faster" against a single municipal site.

## Observability

* **Logs** — structlog; set `LOG_JSON=true`. Every line carries `request_id`, plus `source_id` and
  `job_id` inside ingestion. Secrets are redacted by a processor.
* **Request tracing** — `X-Request-ID` is accepted from the proxy and echoed on every response,
  including errors.
* **Health** — `/api/v1/health/live` for liveness, `/api/v1/health/ready` for readiness
  (database check), `/api/v1/health` for a human-readable component summary.
* **Operational data** — `source_runs`, `ingestion_jobs` and `ingestion_errors` are queryable; the
  API surfaces source health and run history at `/api/v1/sources/{id}/runs`.
* **Statistics** — `/api/v1/statistics` is cached in-process (`STATISTICS_CACHE_SECONDS`).

Suggested alerts: sources in `FAILING`/`OFFLINE`, a rising `ingestion_errors` rate, zero successful
runs in 24 h, readiness failures, p95 latency on `/tenders` and `/search`.

## Reverse proxy

Terminate TLS at the proxy, forward `X-Forwarded-For`/`X-Forwarded-Proto` (the entrypoint enables
`--proxy-headers`), and set request-body and timeout limits there. The API sets no cookies and
holds no session state.

## Backups

* PostgreSQL is the system of record — nightly `pg_dump` plus WAL archiving.
* Blob storage holds retrievable-in-principle documents, but source sites remove files, so back it
  up too if document history matters.
* Restore drill: restore the database, run `alembic upgrade head`, start the API, check
  `/api/v1/health/ready` and `/api/v1/statistics`.

## Deployment checklist

1. `SECRET_KEY` set, `DEBUG=false`, `APP_ENV=production`
2. `CORS_ORIGINS` restricted; `HTTP_USER_AGENT` identifies you with a contact address
3. Migrations applied as a discrete step
4. Geography, categories and connectors seeded; sources imported from **verified** definitions
5. Health checks wired into the orchestrator; log aggregation receiving JSON logs
6. Backups configured and a restore tested
7. Rate limiting / API keys implemented if the API is public (this build ships neither — see
   [SECURITY.md](SECURITY.md))
