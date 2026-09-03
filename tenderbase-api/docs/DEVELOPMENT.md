# Development

## Requirements

* Python 3.11+
* PostgreSQL 14+ (production and full-fidelity local work)
* Redis 7+ (only for background workers)
* Optional: Docker + Compose, Playwright (`browser` extra), Tesseract/poppler (`ocr` extra)

The **test suite needs none of these** — it runs on SQLite in memory with mocked HTTP.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # edit SECRET_KEY and DATABASE_URL
alembic upgrade head
uvicorn app.main:app --reload
```

Or the whole stack:

```bash
docker compose -f docker/docker-compose.yml up --build
```

## Configuration

All configuration is environment variables (`app/config.py`, pydantic-settings). Nothing is
hard-coded; there are no default credentials. Highlights:

| Variable | Default | Notes |
| --- | --- | --- |
| `APP_ENV` | `development` | `production` forbids `DEBUG=true` and the placeholder secret |
| `SECRET_KEY` | placeholder | Must be replaced outside development |
| `DATABASE_URL` | — | Async SQLAlchemy URL; the sync URL for Alembic is derived |
| `REDIS_URL` | `redis://localhost:6379/0` | Workers only |
| `HTTP_*` | see `.env.example` | Timeouts, retries, size caps, user agent, robots, rate limit |
| `HTTP_ALLOW_PRIVATE_NETWORKS` | `false` | SSRF guard; `true` only in tests |
| `OCR_ENABLED` | `false` | See DOCUMENTS.md |
| `AI_ENABLED` | `false` | Core API must start without any AI credentials |
| `STATISTICS_CACHE_SECONDS` | `60` | `0` disables the statistics cache (tests use `0`) |

## Operator scripts

| Command | Purpose |
| --- | --- |
| `python -m scripts.import_geography --provinces` | Load the 9 provinces from bundled, sourced reference data |
| `python -m scripts.import_geography --municipalities path.csv` | Import municipalities from an authoritative CSV (`code,name,type,province_code,district_code,official_website`) |
| `python -m scripts.import_sources sources.json` | Upsert source definitions by slug; validates URLs and connector keys |
| `python -m scripts.seed_categories` | Seed the 15-category taxonomy (idempotent) |
| `python -m scripts.sync_connectors` | Mirror the connector registry into `source_connectors` |
| `python -m scripts.run_ingestion --slug X [--dry-run]` | Run one source (or `--due`, `--source-id`) |
| `python -m scripts.load_dev_fixtures [--purge]` | Clearly-marked development fixtures; refuses to run in production |

Source definition file (`scripts/import_sources.py`):

```json
[
  {
    "slug": "example-municipality-tenders",
    "name": "Example Municipality — tenders page",
    "organization": "Example Local Municipality",
    "base_url": "https://www.example.gov.za",
    "source_type": "MUNICIPAL_TENDER",
    "procurement_scope": "MUNICIPAL",
    "municipality_code": "ZZ000",
    "connector_key": "html.listing",
    "config": { "listing_paths": ["/tenders"], "item_selector": "table tbody tr" },
    "notes": "Verified manually on 2026-09-02; robots.txt allows /tenders.",
    "verified_at": "2026-09-02"
  }
]
```

Only add a source you have actually checked. The importer validates the URL and connector key, but
it cannot verify that a URL is real — that is the operator's job.

## Testing

```bash
pytest                    # everything (219 tests)
pytest tests/unit
pytest tests/connectors
pytest tests/integration
pytest -k dedup -vv
```

Layout and conventions:

| Directory | Scope | Rules |
| --- | --- | --- |
| `tests/unit` | Pure logic: dates, hashing, URLs, text, normalizer, validator, versioning, pagination, documents, config/AI | No I/O |
| `tests/connectors` | Every connector against saved fixtures via `httpx.MockTransport` | **Never** contacts a live site |
| `tests/integration` | API endpoints, the full pipeline, deduplication, Alembic migrations | SQLite in memory (or a temp file for migrations) |

Fixtures available from `tests/conftest.py`: `engine`, `session`, `client`, `province`,
`municipality`, `source`, `make_opportunity`, `fixture_loader`, `mock_fetcher`.

`mock_fetcher` builds a real `HTTPFetcher` over a route map:

```python
fetcher = mock_fetcher({"https://example.org/tenders": (200, html, "text/html")})
```

All fixture data is synthetic, labelled `TEST FIXTURE`, and uses `example.org`. Never commit a real
municipality's page or a real tender as a fixture.

## Code style

* `ruff check .` and `ruff format .` (line length 100, `E,F,I,UP,B`)
* `mypy app`
* Type hints everywhere; `from __future__ import annotations` at the top of every module
* Docstrings explain *why*, not *what*
* No route handler longer than a screen; no business logic in routes
* Never expose a SQLAlchemy model through the API

## Adding things

**A new endpoint** — schema in `app/schemas/`, method on the relevant service, thin handler in
`app/api/v1/routes/`, integration test asserting the envelope and the error cases.

**A new connector** — see [CONNECTORS.md](CONNECTORS.md). Prefer configuration over code.

**A schema change** — edit the model, run `alembic revision --autogenerate -m "..."`, read the
generated migration (autogenerate is a first draft, not an oracle), then
`pytest tests/integration/test_migrations.py`.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `UnsafeURLError` in local tests | `HTTP_ALLOW_PRIVATE_NETWORKS=false` and you targeted localhost |
| Tests are slow | Per-host rate limiting; tests set `HTTP_DEFAULT_RATE_LIMIT_PER_MINUTE=6000` |
| `QUEUE_UNAVAILABLE` | Redis is not running; only worker features need it |
| Search returns no `score` | You are on SQLite; ranking is PostgreSQL-only by design |
