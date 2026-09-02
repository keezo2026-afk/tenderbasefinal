# TenderBase API

A **normalized South African public procurement intelligence data platform** — API only.

TenderBase collects publicly available procurement information (tenders, RFQs, quotations,
addenda and award notices) from municipal, provincial and national sources, normalizes it into a
single canonical model, deduplicates and versions it, extracts text from the attached documents,
and serves the result over a clean, documented REST API.

There is **no frontend in this repository** — no HTML pages, no dashboards, no client app. The API
is the product.

---

## What it is (and is not)

| It is | It is not |
| --- | --- |
| A data platform: ingest → normalize → dedupe → version → serve | A scraper that dumps raw HTML |
| A source registry operators curate from verified information | A list of guessed municipal URLs |
| Honest about gaps (`NULL` beats a guess) | A system that fabricates values to look complete |
| Polite: robots-aware, rate-limited, SSRF-guarded | A tool that bypasses logins, CAPTCHAs or paywalls |

**No fabricated data.** The repository ships zero invented municipalities, source URLs, tenders,
contacts or statistics. Geography reference data carries provenance; development fixtures are
explicitly flagged `is_test_fixture=true` and use `example.org` URLs only.

---

## Quick start

### Docker (recommended)

```bash
cp .env.example .env                # then edit SECRET_KEY at minimum
docker compose -f docker/docker-compose.yml up --build
```

The API is then on <http://localhost:8000>, docs on <http://localhost:8000/api/docs>.

### Local (Python 3.11+)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
export DATABASE_URL="postgresql+psycopg://tenderbase:tenderbase@localhost:5432/tenderbase"

alembic upgrade head
python -m scripts.import_geography --provinces      # bundled, sourced reference data
python -m scripts.seed_categories
python -m scripts.sync_connectors

uvicorn app.main:app --reload
```

Background worker (needs Redis):

```bash
arq app.workers.scheduler.WorkerSettings
```

### Tests

```bash
pytest                       # 219 tests, no network, no database server required
pytest tests/unit            # pure logic
pytest tests/connectors      # fixture-driven connector tests
pytest tests/integration     # API, pipeline, dedup and migration tests (SQLite)
```

---

## API surface

Base path `/api/v1`. Interactive docs at `/api/docs` (Swagger UI) and `/api/redoc`; the schema is at
`/openapi.json`.

| Group | Endpoints |
| --- | --- |
| Health | `GET /health`, `/health/live`, `/health/ready` |
| Tenders | `GET /tenders`, `/tenders/{id}`, `/tenders/{id}/documents`, `/tenders/{id}/events`, `/tenders/{id}/versions` |
| Search | `GET /search` |
| Geography | `GET /provinces`, `/provinces/districts`, `/provinces/{identifier}`, `/municipalities`, `/municipalities/{identifier}`, `/municipalities/{identifier}/tenders` |
| Sources | `GET /sources`, `/sources/connectors`, `/sources/{id}`, `/sources/{id}/runs` |
| Documents | `GET /documents`, `/documents/{id}`, `/documents/{id}/versions`, `/documents/{id}/text` |
| Reference | `GET /categories`, `/events`, `/statistics` |

Every list response uses the same envelope:

```json
{
  "data": [ ... ],
  "pagination": { "page": 1, "page_size": 25, "total_items": 143,
                  "total_pages": 6, "has_next": true, "has_previous": false },
  "meta": { "request_id": "0f9c...", "generated_at": "2026-09-02T10:15:00Z" }
}
```

Errors are equally uniform:

```json
{ "error": { "code": "TENDER_NOT_FOUND", "message": "...", "request_id": "0f9c...", "details": {} } }
```

See [docs/API.md](docs/API.md) for the full contract.

---

## Architecture in one picture

```
source registry ─▶ connector ─▶ discovery ─▶ fetch ─▶ parse ─▶ validate ─▶ normalize
                                                                              │
                                          ┌───────────────────────────────────┘
                                          ▼
                                 deduplicate ─▶ version ─▶ persist ─▶ events
                                          │
                                          ├─▶ documents: download ─▶ sha256 ─▶ store ─▶ extract text ─▶ (OCR)
                                          └─▶ optional AI enrichment (off by default)
                                                          │
                                                          ▼
                                        services ─▶ Pydantic schemas ─▶ REST API
```

SQLAlchemy models are never exposed directly: routes call services, services return ORM objects,
and routes serialise them through Pydantic schemas.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Documentation

| Document | Contents |
| --- | --- |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layers, module map, request and ingestion flow, design rules |
| [DATABASE.md](docs/DATABASE.md) | Every table, key columns, constraints, indexes, migrations |
| [API.md](docs/API.md) | Endpoint reference, filters, pagination, envelopes, error codes |
| [CONNECTORS.md](docs/CONNECTORS.md) | Connector contract, built-ins, configuration keys, writing a new one |
| [INGESTION.md](docs/INGESTION.md) | Pipeline stages, dedup layers, versioning, scheduling, health |
| [DOCUMENTS.md](docs/DOCUMENTS.md) | Download, hashing, storage, extraction, OCR policy, classification |
| [DEVELOPMENT.md](docs/DEVELOPMENT.md) | Setup, scripts, testing conventions, code style |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Docker, environment variables, migrations, scaling, observability |
| [SECURITY.md](docs/SECURITY.md) | SSRF guards, crawl ethics, secrets, extension points for auth |
| [BUILD_REPORT.md](BUILD_REPORT.md) | What was built, what was verified, honest known limitations |

---

## Project layout

```
app/
  api/v1/routes/     HTTP layer only — thin handlers
  connectors/        source adapters (+ custom/etender.py)
  ingestion/         fetch, parse, validate, normalize, dedupe, version, pipeline
  documents/         download, storage, extraction, OCR, classification
  search/            pluggable search backend (PostgreSQL FTS, portable fallback)
  services/          business logic between models and schemas
  db/models/         SQLAlchemy 2.x models
  schemas/           Pydantic v2 request/response models
  workers/           ARQ queue, tasks and cron schedule
  ai/                optional enrichment provider (disabled by default)
migrations/          Alembic revisions
scripts/             operator CLIs (geography, sources, categories, ingestion, fixtures)
tests/               unit, connectors (fixture-driven), integration
docker/              Dockerfile, compose stack, entrypoint
docs/                the documentation set above
```

## Licence

Apache-2.0. Ingested data remains subject to the terms of its publishers.
