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
| `python -m scripts.verify_source <id-or-slug> [--discover] [--no-store] [--activate --reason R] [--json]` | Run the evidence-based verification procedure and record the report; `--activate` is refused unless verification passed |
| `python -m scripts.run_report [--source S] [--duplicates] [--json]` | Fleet picture from the database: totals, sources needing attention, recent failed runs, the duplicate review queue |
| `python -m scripts.manage_api_keys list\|stats\|create\|check\|revoke\|rotate ...` | API-key operations (`create` prints the raw key exactly once); the default way to mint, since `POST /api/v1/api-keys` is off by default |

Every script takes its configuration from the same `Settings` as the API (so `DATABASE_URL`,
`HTTP_ALLOWED_PORTS` and `API_KEY_PEPPER` apply uniformly) and prints human-readable output; those that
mutate data also accept `--json` for a pipeline. None of them accept a credentials argument — a key or
password passed on a command line is visible in `ps` and in shell history.

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
pytest                                    # everything (371 tests on PostgreSQL)
pytest tests/unit
pytest tests/connectors
pytest tests/integration
pytest -k dedup -vv

# the same suite on the real engine
TEST_DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/tenderbase_test" pytest
```

**Two backends, one suite.** With no `TEST_DATABASE_URL` the integration tests build their schema on
SQLite (a temp file, because migrations cannot use `:memory:`). Set `TEST_DATABASE_URL` and the fixture
creates a *throwaway database* on the server instead — `CREATE DATABASE … TEMPLATE template0`,
`enable_extension('pg_trgm')`, `alembic upgrade head` → `downgrade base` → `upgrade head` →
`DROP … WITH (FORCE)` — so every migration in the chain is exercised in both directions on the dialect
that ships. Tests that assert PostgreSQL-only behaviour carry `@pytest.mark.postgres`, and Redis-backed
tests carry `@pytest.mark.redis`; each skips when its backend is absent, which is the only kind of skip
the suite allows. Today: **371 passed** on PostgreSQL; **340 passed + 31 skipped** on SQLite (unit 181,
integration 130, connectors 29). The five markers the suite uses are registered in `pyproject.toml`, so
an unregistered name is a warning rather than a silently-ignored annotation.

Write integration tests against the `db_url`-bound engine, not the settings default — a test that builds
its own engine from `get_settings()` silently reads the developer's database.

Layout and conventions:

| Directory | Scope | Rules |
| --- | --- | --- |
| `tests/unit` | Pure logic: dates, hashing, URLs, text, normalizer, validator, versioning, pagination, documents, config/AI, `.env.example` drift | No I/O |
| `tests/connectors` | Every connector against saved fixtures via `httpx.MockTransport` | **Never** contacts a live site |
| `tests/integration` | API endpoints (incl. auth, rate limits, metrics), the full pipeline, source verification, deduplication, Alembic migrations | SQLite, or PostgreSQL with `TEST_DATABASE_URL`; no live network |

Fixtures available from `tests/conftest.py`: `engine`, `session`, `client`, `make_client`,
`province`, `municipality`, `source`, `make_opportunity`, `fixture_loader`, `mock_fetcher`,
`redis_url`, `worker_database`, `require_postgres`.

`make_client(**settings_overrides)` returns an `AsyncClient` for an app built with those settings
(`api_key_enforcement_enabled=True`, a dead `redis_url`, `metrics_token=...`), so a test never mutates
process-global configuration:

```python
client = await make_client(rate_limit_enabled=True, redis_url="redis://127.0.0.1:1/0")
```

The application's own settings stay reachable as `client.app.state.settings` — use them when minting an
API key in a test, or the digest will not match what the app verifies against. Its lifespan is *not*
run (it owns the global engine and limiter, which the fixtures manage instead), so a test that needs the
limiter installs one with `build_limiter`/`install_limiter` and restores it afterwards.

`mock_fetcher` builds a real `HTTPFetcher` over a route map:

`mock_fetcher` builds a real `HTTPFetcher` over a route map:

```python
fetcher = mock_fetcher({"https://example.org/tenders": (200, html, "text/html")})
```

All fixture data is synthetic, labelled `TEST FIXTURE`, and uses `example.org`. Never commit a real
municipality's page or a real tender as a fixture.

Where a test needs a *live-ish* server (source verification, redirect and robots behaviour), it runs a
`ThreadingHTTPServer` bound to `127.0.0.1` inside the test process — see
`tests/integration/test_source_verification.py`. Such a server must bind **8080 or 8443**: the URL
policy rejects any other port before it looks at addresses, so an ephemeral-port fixture fails for a
reason that has nothing to do with the behaviour under test (and would tempt someone into weakening the
SSRF guard). Do the same in your tests; do not disable the guard.

## Code style

* `ruff check .` and `ruff format --check .` (line length 100, `E,F,I,UP,B`) — both clean across the
  repository, so `ruff format .` is a formatting command and never a diff you have to review
* `mypy app` — clean over the whole application package (105 modules). Not decorative: the type checker
  is what surfaced a `TYPE_CHECKING`-only import pointing at the wrong model module, a
  `str` municipality id reaching a `UUID` parameter, and pool metrics that vanished whenever a session
  was bound to a `Connection`
* Type hints everywhere; `from __future__ import annotations` at the top of every module
* Docstrings explain *why*, not *what*
* No route handler longer than a screen; no business logic in routes
* Never expose a SQLAlchemy model through the API
* **Never raise from middleware.** An exception raised inside a `BaseHTTPMiddleware` dispatch escapes
  the application's exception handlers, so a deliberate refusal reaches the client as a 500. Return
  `app.api.errors.error_response(...)` instead — that is how `PublicRateLimitMiddleware` answers 429
* **A script's stdout is its result.** Services log to stdout (what the container runtime collects);
  `scripts/*.py` call `configure_logging(stream=sys.stderr)` so `--json` stays parseable
* A list endpoint's filter model is built with `parse_query_filter`, not by calling the model
  directly — see the module docstring for why the direct call turns a client typo into a 500

## Adding things

**A new endpoint** — schema in `app/schemas/`, method on the relevant service, thin handler in
`app/api/v1/routes/`, integration test asserting the envelope and the error cases.

**A new connector** — see [CONNECTORS.md](CONNECTORS.md). Prefer configuration over code.

**A schema change** — edit the model, run `alembic revision --autogenerate -m "..."`, read the
generated migration (autogenerate is a first draft, not an oracle), then
`pytest tests/integration/test_migrations.py` and `alembic check`.

Revisions that create *portable* tables — anything before the PostgreSQL-only revision — must use
dialect-neutral server defaults: `server_default=sa.text("(CURRENT_TIMESTAMP)")`, never `sa.text("now()")`.
PostgreSQL accepts both, so a `now()` default looks harmless and passes CI; a SQLite database built by
that migration then refuses its first insert with `unknown function: now()`, and the failure appears
wherever the dev database was created by `alembic upgrade head` rather than by `create_all`.
`test_portable_revisions_emit_portable_defaults` reads the DDL that the migrations actually produced and
`test_migrated_schema_inserts_rows_with_defaults` inserts a real row through it.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `UnsafeURLError` in local tests | `HTTP_ALLOW_PRIVATE_NETWORKS=false` and you targeted localhost |
| Tests are slow | Per-host rate limiting; tests set `HTTP_DEFAULT_RATE_LIMIT_PER_MINUTE=6000` |
| `QUEUE_UNAVAILABLE` | Redis is not running; only worker features need it |
| Search returns no `score` | You are on SQLite; ranking is PostgreSQL-only by design |
