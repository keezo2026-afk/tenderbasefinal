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
* Extensible string enums with tolerant parsing (unknown publisher values → `UNKNOWN`/`OTHER`).

### Data layer
* 17 SQLAlchemy 2.x models with UUID PKs, FKs with explicit `ON DELETE`, unique constraints, check
  constraints, timestamps and 65 indexes.
* Dialect-portable column types (`UUID`→`String(36)`, `JSONB`→`JSON` on SQLite) so migrations and
  the full test suite run without a database server.
* Two Alembic revisions: portable initial schema + PostgreSQL-only FTS/trigram objects.

### Ingestion
* Guarded HTTP fetcher: SSRF validation (including after each redirect), scheme allowlist, byte
  caps, timeouts, retries with exponential backoff + jitter, per-host rate limiting, robots.txt.
* Connector registry with six connectors; source behaviour is data-driven via JSON config.
* Validator, normalizer (SA date formats, ZAR money, reference numbers, enums, text cleaning),
  four-layer deduplicator, version engine with semantic events, and the orchestrating pipeline
  with per-item and per-source failure isolation.
* Source health derivation and backoff; `source_runs`, `ingestion_jobs`, `ingestion_errors`.

### Documents
* SHA-256 content addressing, sharded storage keys, `BlobStorage` ABC + local implementation with
  traversal guards, conditional re-download, document versioning.
* Native-first text extraction (PDF/HTML/TXT/CSV/DOCX) with provenance; OCR strictly gated behind
  a config flag and a scanned-page heuristic; rule-based document classification.

### API
* 26 endpoints under `/api/v1` plus `/`, `/api/docs`, `/api/redoc`, `/openapi.json`.
* Uniform `data`/`pagination`/`meta` and error envelopes, `X-Request-ID` echo, typed query schemas,
  bounded deterministic pagination with stable tiebreakers.
* Six services between models and schemas; no SQLAlchemy object is ever serialised directly.

### Operations
* ARQ worker (queue, tasks, cron schedule), six operator scripts, Dockerfile + Compose stack +
  role-based entrypoint, ten documentation files.

---

## 2. Database tables (17)

`provinces`, `districts`, `municipalities`, `municipality_sources`, `source_connectors`,
`source_runs`, `procurement_opportunities`, `opportunity_versions`, `opportunity_events`,
`contacts`, `categories`, `opportunity_categories`, `documents`, `document_versions`,
`document_text`, `ingestion_jobs`, `ingestion_errors`.

Verified: `alembic upgrade head` and `downgrade base` both run cleanly (SQLite), and the migrated
schema contains every table and column defined by the models.

---

## 3. API endpoints (26)

| Group | Endpoints |
| --- | --- |
| Health (3) | `/health`, `/health/live`, `/health/ready` |
| Tenders (5) | `/tenders`, `/tenders/{id}`, `/tenders/{id}/documents`, `/tenders/{id}/events`, `/tenders/{id}/versions` |
| Search (1) | `/search` |
| Geography (6) | `/provinces`, `/provinces/districts`, `/provinces/{identifier}`, `/municipalities`, `/municipalities/{identifier}`, `/municipalities/{identifier}/tenders` |
| Sources (4) | `/sources`, `/sources/connectors`, `/sources/{id}`, `/sources/{id}/runs` |
| Documents (4) | `/documents`, `/documents/{id}`, `/documents/{id}/versions`, `/documents/{id}/text` |
| Reference (3) | `/categories`, `/events`, `/statistics` |

Plus `/`, `/api/docs`, `/api/redoc`, `/openapi.json`.

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

**219 tests, all passing**, in ~8 seconds, with no network access and no database server.

| Suite | Tests | Coverage of behaviour |
| --- | --- | --- |
| `tests/unit` | 127 | dates, hashing/fingerprints, URL/SSRF policy, text, normalizer, validator, versioning, pagination & query validation, documents (storage keys, sniffing, extraction, classification), config/logging redaction/AI abstraction |
| `tests/connectors` | 29 | registry behaviour and all five non-browser connectors against saved fixtures via `httpx.MockTransport` |
| `tests/integration` | 63 | health & envelope, tenders/search/geography/categories/events/statistics endpoints, sources & documents endpoints, four-layer deduplication, end-to-end pipeline (create → idempotent re-run → change detection → versions/events → health), Alembic upgrade/downgrade |

Run: `pytest` (or per directory). Connector tests never contact a live site; every fixture is
synthetic, marked `TEST FIXTURE`, and uses `example.org`.

Bugs found and fixed **because** the tests were run: word-boundary truncation in `utils/text`,
detail-page document extraction in `html.listing`, WordPress fallback not triggering on a permanent
fetch error, a per-source rate-limit default that ignored settings, a process-wide statistics cache
with no way to disable it, and an Alembic env that ignored an explicitly supplied URL.

---

## 6. What was actually executed in this environment

| Verified | How |
| --- | --- |
| Application imports and app factory builds | `python -c "import app.main"` |
| OpenAPI schema generation, all 26 routes present | integration test |
| Migrations upgrade **and** downgrade | `alembic upgrade head`, integration test |
| Full pipeline against fixture HTML | integration test (create/update/skip paths) |
| All six connectors registered; five parsed against fixtures | connector tests |
| 219-test suite green | `pytest` |

| **Not** executed | Why |
| --- | --- |
| PostgreSQL-specific SQL (FTS, `pg_trgm`, `JSONB`) | No PostgreSQL available in the build environment |
| Docker image build / Compose stack | No Docker daemon available |
| Redis/ARQ worker loop | No Redis available |
| Playwright browser connector | Chromium not installed |
| OCR path | Tesseract/poppler not installed |
| Any live municipal or eTender request | Prohibited by the brief and by the crawl-ethics rules |

---

## 7. Known limitations (honest list)

1. **PostgreSQL paths are unexercised here.** The FTS/trigram migration, `ts_rank` ranking and
   trigram dedup layer are written to the documented PostgreSQL behaviour but have never been run
   against a live server in this environment. They must be validated before production use.
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
8. **No auth, API keys or rate limiting.** Read-only API; extension points prepared, nothing
   implemented. Do not expose it publicly without a gateway.
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

1. Stand up PostgreSQL and Redis; run the suite against PostgreSQL and validate the FTS/trigram
   migration, ranking quality and the layer-4 dedup thresholds with real text.
2. Build the Docker image and bring up the Compose stack; fix the inevitable first-build issues and
   add a CI job that builds the image and runs `pytest`.
3. Import the authoritative municipality dataset and onboard a **small** set (5–10) of manually
   verified municipal sources; observe run history, health transitions and error rates.
4. Verify the eTender OCDS endpoint against its published API documentation, then enable the
   connector with real configuration and record the verification date in the source notes.
5. Run the document pipeline end to end on real PDFs: measure native-extraction success, tune the
   OCR trigger, and evaluate classification accuracy against a labelled sample.
6. Add API keys + rate limiting at the gateway (or in the prepared middleware hook) before any
   public exposure, and add per-endpoint latency metrics.
7. Review dedup outcomes on live data — particularly re-advertised tenders — and adjust the
   `UNCERTAIN` review workflow with a real reviewer in the loop.
