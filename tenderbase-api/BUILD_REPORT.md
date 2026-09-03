# Build report — TenderBase API

**Date:** 2026-09-02 · **Repository path:** `tenderbase-api/` · **Version:** 0.1.0
**Stack:** Python 3.11, FastAPI, SQLAlchemy 2.x, Alembic, Pydantic v2, PostgreSQL, Redis/ARQ,
httpx, BeautifulSoup4/lxml, pypdf/pdfplumber, pytest.

This report is deliberately blunt about what was built, what was actually executed, and what was
not. Nothing below is aspirational.

---

## 1. What was implemented

### Application core
* Environment-driven configuration (`app/config.py`) that **refuses to start in production** with
  a placeholder secret or `DEBUG=true`; AI credentials optional.
* Structured logging (structlog) with `request_id` / `source_id` / `job_id` context vars and a
  secret-redaction processor.
* Error hierarchy mapping domain errors to stable API error codes; single error envelope.
* API-key authentication with scopes, an immediate-revocation check, and Redis rate limiting with a
  per-process fallback — all in one dependency (`api_access`) attached to the router, not to each
  handler, so a new route cannot be added without a security check by forgetting a decorator.
* Extensible string enums with tolerant parsing (unknown publisher values → `UNKNOWN`/`OTHER`).

### Data layer
* 18 SQLAlchemy 2.x models (108 indexes, 15 check constraints on PostgreSQL) with UUID PKs, FKs with
  explicit `ON DELETE`/`RESTRICT`, unique constraints, timestamps and comments on the non-obvious.
* Dialect-portable column types (`UUID`→`String(36)`, `JSONB`→`JSON` on SQLite) so migrations and
  the full test suite run without a database server.
* Four Alembic revisions: portable initial schema → PostgreSQL-only FTS/trigram objects → source
  lifecycle + `api_keys` → a corrected verification check constraint. Every revision is exercised
  forward and backward (`upgrade head` → `downgrade base` → `upgrade head`) against a throwaway
  PostgreSQL database created by the test fixture, and `alembic check` reports no drift.

### Ingestion
* Guarded HTTP fetcher: SSRF validation (including after each redirect), scheme allowlist, byte
  caps, timeouts, retries with exponential backoff + jitter, per-host rate limiting, robots.txt.
* Connector registry with six connectors; source behaviour is data-driven via JSON config.
* Validator, normalizer (SA date formats, ZAR money, reference numbers, enums, text cleaning),
  four-layer deduplicator, version engine with semantic events, and the orchestrating pipeline
  with per-item and per-source failure isolation.
* Source health derivation and backoff; `source_runs`, `ingestion_jobs`, `ingestion_errors`.
* Evidence-based source verification (`app/ingestion/verifier.py`): six required checks and four
  optional ones, each with recorded evidence, so "we fetched a page" and "this source works" are
  different claims. A `200` with nothing parseable in it is a **failure**, not a pass.

### Documents
* SHA-256 content addressing, sharded storage keys, `BlobStorage` ABC + local implementation with
  traversal guards, conditional re-download, document versioning.
* Native-first text extraction (PDF/HTML/TXT/CSV/DOCX) with provenance; OCR strictly gated behind
  a config flag and a scanned-page heuristic; rule-based document classification.

### API
* 37 documented operations under `/api/v1` (36 paths), plus `/`, `/api/docs`, `/api/redoc`,
  `/openapi.json`, `/metrics`, and the three health probes mirrored at the application root.
* Uniform `data`/`pagination`/`meta` and error envelopes, `X-Request-ID` echo, typed query schemas,
  bounded deterministic pagination with stable tiebreakers.
* Six services between models and schemas; no SQLAlchemy object is ever serialised directly.

### Operations
* ARQ worker (queue with unique/delayed jobs, cron schedule, bounded retries with jitter for transient
  failures only), nine operator scripts, Prometheus metrics owned by the application
  (`app/observability/metrics.py`), Dockerfile + Compose stack + role-based entrypoint, and the
  documentation set in `docs/`.

---

## 2. Database tables (18)

`provinces`, `districts`, `municipalities`, `municipality_sources`, `source_connectors`,
`source_runs`, `procurement_opportunities`, `opportunity_versions`, `opportunity_events`,
`contacts`, `categories`, `opportunity_categories`, `documents`, `document_versions`,
`document_text`, `ingestion_jobs`, `ingestion_errors`, `api_keys`.

On PostgreSQL that is 108 indexes and 15 check constraints — the constraints are part of the model,
not documentation: `version >= 1`, `estimated_value >= 0`, `closing_at > published_at`, lifecycle and
verification status domains, `rate_limit_per_minute > 0`, `revoked_keys_are_stamped`, and
`passed_verification_is_dated` (a passing verification must carry
`verification_at`, so a source cannot claim to be verified with no recorded evidence).

Deliberately absent: any table that would exist only to make the schema look complete — no generic
`metadata`/`settings`/`audit_log` catch-alls. The two verification timestamps are separate columns
because they are separate facts: `verification_at` is what the automated procedure observed,
`verified_at` is a human signing off, and no code sets the latter.

---

## 3. API endpoints

37 documented operations, all under `/api/v1`, plus four infrastructure routes and three root aliases.

| Group | Endpoints |
| --- | --- |
| Health (3) | `/health`, `/health/live`, `/health/ready` — also mounted at the root, unauthenticated |
| Tenders (5) | `/tenders`, `/tenders/{id}`, `/tenders/{id}/documents`, `/tenders/{id}/events`, `/tenders/{id}/versions` |
| Search (1) | `/search` |
| Geography (6) | `/provinces`, `/provinces/districts`, `/provinces/{identifier}`, `/municipalities`, `/municipalities/{identifier}`, `/municipalities/{identifier}/tenders` |
| Sources (4) | `/sources`, `/sources/connectors`, `/sources/{id}`, `/sources/{id}/runs` |
| Documents (4) | `/documents`, `/documents/{id}`, `/documents/{id}/versions`, `/documents/{id}/text` |
| Reference (3) | `/categories`, `/events`, `/statistics` |
| Operations (6) | `/operations/sources/{id}/report`, `/history`, `/verification`, `/operations/runs/failed`, `/operations/sources/unhealthy`, `/operations/duplicates/review` |
| API keys (5) | `GET /api-keys`, `/api-keys/summary`, `/api-keys/{id}` · `POST /api-keys`, `/api-keys/{id}/revoke` |

Plus `/` (service banner), `/api/docs`, `/api/redoc`, `/openapi.json`, and `/metrics` (Prometheus text,
excluded from the OpenAPI document and optionally bearer-token gated).

Data endpoints sit behind one dependency (`api_access`) that authenticates, checks the scope derived
from the path, records last-use metadata and applies the rate limit; the probes and the schema are the
only unauthenticated routes, on purpose.

---

## 4. Connectors (6)

| Key | Type | Status |
| --- | --- | --- |
| `html.listing` | HTML | Implemented and fixture-tested (listing, pagination, detail following, document extraction, failure degradation) |
| `http.json` | HTTP | Implemented and fixture-tested (field mapping, attachments, non-JSON rejection) |
| `wordpress.rest` | WordPress | Implemented and fixture-tested, including HTML fallback when the REST API is disabled |
| `pdf.repository` | PDF | Implemented and fixture-tested (PDF-link discovery, non-PDF links ignored) |
| `browser.playwright` | Browser | Implemented; **not executed** — Playwright/Chromium are not installed in this environment |
| `custom.etender_ocds` | Custom | OCDS 1.1 parser implemented and fixture-tested; **no endpoint hard-coded**, live contract unverified |

No per-municipality connector files exist, by design.

---

## 5. Tests

**371 tests.** PostgreSQL: 371 passed. SQLite: 340 passed + 31 skipped — every skip is an absent
capability (PostgreSQL-only SQL, a Redis server, Tesseract), never a skipped assertion. Roughly 33 s on
SQLite, 52 s on PostgreSQL.

The same commands gate every change: `ruff check .`, `ruff format --check .` and `mypy app` are all
clean (the type checker reached 0 errors from 76 while the defects below were being fixed — several of
those errors *were* the defects).

| Suite | Tests | Covers |
| --- | --- | --- |
| `tests/unit` | 181 | dates, hashing/fingerprints, URL & SSRF policy, text, normalizer, validator, versioning, pagination and query validation, documents (storage keys, sniffing, extraction, classification), config, API-key primitives (scope parsing, key shape, hashing), log destination/format/redaction, log redaction, AI abstraction, worker retry policy, rate-limit windows, `.env.example`/compose drift |
| `tests/connectors` | 29 | registry behaviour and all five non-browser connectors against saved fixtures via `httpx.MockTransport` |
| `tests/integration` | 130 (+31 skipped) | envelopes and error codes; query-filter and pagination bounds; **authentication, scopes, rate limits, middleware refusal shape, metrics export**; **migration portability** (DDL defaults read back from SQLite plus a real insert) and upgrade/downgrade; the source-verification procedure against a live local HTTP server; endpoints for every resource; four-layer deduplication including the trigram layer's false-positive guards; the end-to-end pipeline (create → idempotent re-run → change detection → versions/events → health); the ARQ queue against a real Redis; Alembic upgrade/downgrade; PostgreSQL-only behaviour |

`TEST_DATABASE_URL` selects the backend. When it points at PostgreSQL the fixture creates a throwaway
database, enables `pg_trgm`, runs `upgrade head → downgrade base → upgrade head`, and drops the database
`WITH (FORCE)` — so migrations are verified in both directions on the shipping dialect, and no developer's
own database is ever involved.

No test contacts a live site. Where a test needs server behaviour (redirects, `robots.txt`, a PDF
behind a listing page), it runs a `ThreadingHTTPServer` bound to `127.0.0.1:8080` inside the test
process and serves fixture HTML from `tests/fixtures/`.

Bugs found and fixed **because the tests were run** — each now has a regression test:

* word-boundary truncation in `utils/text`; detail-page document extraction in `html.listing`;
  WordPress fallback not triggering on a permanent fetch error; a per-source rate-limit default that
  ignored settings; a process-wide statistics cache with no way to disable it; an Alembic env that
  ignored an explicitly supplied URL.
* **two deduplication defects visible only on PostgreSQL** — the trigram layer matching across
  municipalities, and across conflicting reference numbers. SQLite's different `similarity()` and
  lack of constraint checking hid both; this is why the suite runs on both.
* `verifier._check_targets` did not exist, so any source whose listing *did* parse raised
  `AttributeError` — the failure paths were tested and the success path was not.
* `ck_municipality_sources_passed_verification_is_dated` required the human `verified_at` stamp, so
  recording any passing verification raised `IntegrityError`; only a persisting test found it.
* `validate_url`'s port policy was unconfigurable in practice: the signature accepted `allowed_ports`
  and no call site ever passed one.
* the verification path handed `str` municipality/province ids to a normalizer parameter typed
  `UUID | None`, so its geo resolution silently produced nothing.
* `InsufficientScopeError` (403) was swallowed into a 503 "authentication temporarily unavailable",
  because the auth dependency caught only `AuthenticationError`.
* readiness reported the cache as `disabled` instead of probing it when Redis was degradable, and
  honoured `RATE_LIMIT_FAIL_OPEN=false` only if Redis broke *after* startup.
* `docker-compose.yml` set `STORAGE_LOCAL_PATH` — a variable the application does not read — so
  documents and raw payloads were written to the container filesystem instead of the mounted volume.
* **an invalid query parameter was served as 500.** Filter models are built inside dependencies, where
  FastAPI does not translate them, so `?status=BOGUS` or a reversed date range raised
  `pydantic.ValidationError` and became `INTERNAL_ERROR` — a client typo reported as a server fault, in
  monitoring and to the caller. `parse_query_filter` now maps them to 422 with one entry per field.
  The same fix exposed that `MAX_PAGE_SIZE` was dead in one direction (a hard-coded `le=100` in the query
  annotation refused anything above 100 however the operator configured it) and dishonest in the other
  (a *lower* maximum silently returned fewer rows than the client asked for).
* **the rate limiter's refusals were 500s too**: `PublicRateLimitMiddleware` *raised*
  `RateLimitedError`, and an exception raised inside a `BaseHTTPMiddleware` escapes the exception
  handlers. It now returns the shared error response.
* **middleware order.** `add_middleware` prepends, so the limiter had ended up outermost: its 429s
  carried no `X-Request-ID` and no security headers, and were not counted by
  `tenderbase_http_requests_total` — a rate-limit storm would have looked like zero traffic.
* **structlog captured its configuration at import time**, and modules create their loggers then. Two
  consequences: a script's `--json` stdout could be preceded by a log line (unparsable output), and —
  far more serious — lines from early-imported modules were rendered with structlog's *defaults*, i.e.
  **without redaction, without `request_id`, and in console format in a JSON deployment**. The
  destination, level and renderer are now resolved per write.
* **a migration emitted a PostgreSQL-only default in a portable revision** (`server_default=now()` for
  `api_keys.created_at/updated_at`), so a SQLite database built by `alembic upgrade head` — the documented
  development path — refused its first insert with `unknown function: now()`. PostgreSQL was unaffected,
  so no PostgreSQL-only run would ever have shown it.
* `scripts/manage_api_keys.py create --scopes read:tenders,read:statistics` failed with "Unknown scope":
  argparse had already turned the argument into a list, and only the *string* form was comma-split.
* `lifespan(app)` did `import app.connectors`, which rebinds the name `app` inside that function to the
  package — every later `app.state` would have read the wrong object.
* `workers/retry.py`'s `TYPE_CHECKING` import pointed at `app.db.models.opportunity` for `SourceRun`,
  which lives in `models.source` (harmless at runtime, fatal the moment the annotation is resolved).
* Pool gauges disappeared whenever a session was bound to a `Connection` (`Connection` has no `.pool`),
  and `/metrics` sampled the process-global engine instead of the database actually serving the request.

---

## 6. What was actually executed in this environment

| Verified | How |
| --- | --- |
| Application imports and app factory builds | `python -c "import app.main"` |
| OpenAPI schema generation, all documented operations present | integration test |
| Migrations upgrade **and** downgrade | `alembic upgrade head`, integration test |
| Full pipeline against fixture HTML | integration test (create/update/skip paths) |
| Source verification against a real HTTP server | 18 tests: 200-but-empty, robots refusal, 401, off-site links, unknown connector, dead port |
| Authentication, scopes, 429s and `/metrics` over ASGI HTTP | 22 tests |
| All six connectors registered; five parsed against fixtures | connector tests |
| 371-test suite green on **two database backends** | `pytest` with and without `TEST_DATABASE_URL` |
| Lint, format and type gates | `ruff check .`, `ruff format --check .` (165 files), `mypy app` (105 modules, 0 errors), `alembic check` (no drift) |
| Application served for real and answered live | `uvicorn app.main:app` bound to 0.0.0.0 against PostgreSQL; health, `/metrics`, `/api/v1/tenders` and 401-for-anon checked with HTTP requests |
| Rate limiting enforced through the real server, not only ASGI | 46 sequential requests to `/metrics` through Redis-backed enforcement: 39 × 200 then 429 with `Retry-After: 46`, `X-RateLimit-{Limit,Remaining,Reset,Policy}`, `X-Request-ID`, the security headers and the standard error envelope |
| Machine-readable script output | `manage_api_keys create/check/rotate --json` and `run_report --json` parsed with `json.load` against a migration-built SQLite database (logs on stderr) |
| Worker loop against real Redis | `arq` + `tests/integration/test_worker_queue.py` (enqueue, retry-with-backoff, permanent-failure path, job rows) |
| Every operator script executed | geography import, category seed, connector sync (+`--dry-run`), dev fixtures, ingestion run, `verify_source --no-store`, `run_report`, `manage_api_keys` create/check/rotate/revoke |
| Migrations on a live server | `alembic upgrade head`, `downgrade base`, `upgrade head`, `alembic check` (no drift) |

| **Not** executed | Why |
| --- | --- |
| Docker image build / Compose stack | No Docker daemon in this environment. `docker/` files are lint-checked by tests that parse them (`test_deployment_artifacts.py`), which found and fixed a storage-path bug — but the image itself has never been built here |
| Playwright rendering, Tesseract OCR | Not installed, and the browser download host is unreachable from this sandbox |
| Any live municipal or eTender request | There is no outbound HTTP route from this sandbox (direct probes to `durban.gov.za`, `capegateway.org.za` and `etenderportal.gov.za` all return no connection), and the crawl-ethics rules prohibit circumvention in any case |

---

## 7. Known limitations (honest list)

1. **Search and dedup quality are unmeasured on real data.** The FTS ranking, `pg_trgm` layer and the
   `0.82`/`0.65` thresholds now execute against PostgreSQL and behave correctly on fixture text, but
   nobody has measured precision/recall against a corpus of real, re-advertised tenders. That tuning
   is the main open work before trusting dedup at scale.
2. **Docker artefacts are unbuilt.** The Dockerfile, entrypoint and Compose stack are written but
   never executed; expect at least one iteration when first built.
3. **eTender is unverified.** The OCDS parser follows the published schema and is fixture-tested,
   but the endpoint, paging parameters and rate limits of the live portal are not confirmed. The
   connector refuses to run without an operator-supplied endpoint, on purpose.
4. **Zero real sources ship.** `municipality_sources` is empty and only the nine provinces are
   bundled (with provenance). Municipalities and sources must be imported by an operator from
   authoritative data. This is a deliberate refusal to fabricate coverage.
5. **The browser connector is untested at runtime.** It requires the optional extra plus a
   Chromium install.
6. **OCR is interface-only.** The trigger heuristic and engine abstraction exist; the concrete
   Tesseract engine reports itself unavailable until the system packages are installed.
7. **AI enrichment is a null implementation.** Vendor adapters raise `NotImplementedError` rather
   than fake an integration; nothing in the platform depends on AI.
8. **AuthN/AuthZ exists but is not a full IAM story.** Keys carry scopes and expire; there are no IP
   bindings, no per-key quotas or usage accounting, no rotation automation, and no audit sink beyond
   structured logs. `/metrics` is open unless `METRICS_TOKEN` is set — which is correct on a private
   network and wrong on a public one. A gateway in front remains a good idea, not a requirement.
9. **`fingerprint` is globally unique.** Dedup resolves matches before insert, but two genuinely
   distinct records that collide on title+organization+closing date+type would raise an integrity
   error rather than being stored side by side. Worth revisiting if collisions appear in the wild.
10. **Legacy `.doc` and Excel extraction are not implemented** — they return empty text with method
    `NONE` rather than a guess.
11. **Statistics caching is per-process**, not shared across replicas; heavier aggregation should
    move to materialised views as the dataset grows.
12. **The repository root also contains an unrelated legacy `tenderbasedesign.html`** from before
    this build. It was left untouched, as instructed, and is not part of the API.

---

## 8. External integrations required before production

| Integration | Needed for | Status |
| --- | --- | --- |
| PostgreSQL 14+ with `pg_trgm` | Primary datastore, full-text and fuzzy dedup | Required; not available here |
| Redis 7+ | ARQ queue and scheduling | Required for ingestion; API works without it |
| Object storage (S3-compatible) | Multi-host document storage | Optional; `BlobStorage` ABC ready, local backend ships |
| Tesseract + poppler | OCR of scanned adverts | Optional; disabled by default |
| Playwright/Chromium | JavaScript-rendered listings | Optional |
| AI provider (OpenAI/Anthropic/local) | Summaries, categorisation assistance | Optional; null provider by default |
| Authoritative municipality dataset (Municipal Demarcation Board / StatsSA) | Geography beyond provinces | **Required** — nothing invented ships |
| National Treasury eTender API documentation | Enabling `custom.etender_ocds` | **Required** — no endpoint guessed |

---

## 9. Suggested next sprint

The first two items from the previous plan are done — PostgreSQL and Redis were stood up, the suite and
migrations were validated on them, and `docker/` is at least parse-checked by the test suite. What is
left, in order of consequence:

1. **Onboard real sources.** Import 5–10 manually checked municipal portals plus the eTender OCDS API,
   run `verify_source` on each, and let ingestion run for a week. Everything else in this list depends
   on having real data to measure.
2. **Tune dedup and search on that data.** Precision/recall for the trigram layer, `ts_rank` relevance,
   and the `UNCERTAIN` review workflow with an actual reviewer in the loop.
3. **Build the image and run the Compose stack** for real (the sandbox has no Docker daemon), then add
   CI: image build, `pytest` on PostgreSQL, `alembic check`, `ruff check`.
4. **Verify the eTender live contract** (endpoint, paging, rate limits) against its published swagger
   doc, then fill in `custom.etender_ocds` source config and record the verification date in the
   source notes.
5. **Run the document pipeline on real PDFs:** measure native-extraction success, tune the OCR trigger,
   and evaluate classification against a labelled sample. Requires installing Tesseract/poppler.
6. **S3 object storage backend** behind `BlobStorage` (the ABC and key scheme are already correct for
   it), and revisit the globally-unique `fingerprint` constraint if collisions appear.
7. **Materialise the statistics** the dashboard queries repeat, if `/statistics` shows up in a profile
   once the dataset is large; and consider a push-based metrics sink if scraping is not available.

