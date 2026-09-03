# Architecture

TenderBase is a pipeline with an API on the end. Every stage is a separate module with one
responsibility, so a change in one municipality's HTML cannot break parsing for another, and a
change in the parser cannot corrupt what is already stored.

```
                     ┌──────────────────────────────────────────────────────┐
                     │                  source registry                     │
                     │      municipality_sources + source_connectors        │
                     └───────────────────────┬──────────────────────────────┘
                                             │ SourceContext
                                             ▼
   ┌──────────┐   discover   ┌──────────┐  fetch  ┌──────────┐  parse  ┌──────────┐
   │ connector│─────────────▶│ targets  │────────▶│ responses│────────▶│ RawItem  │
   └──────────┘              └──────────┘         └──────────┘         └────┬─────┘
                                                                            │
        validate ◀───────────────────────────────────────────────────────────┘
            │
            ▼
       normalize ──▶ NormalizedOpportunity ──▶ deduplicate ──▶ version ──▶ persist
                                                    │             │
                                                    │             └─▶ opportunity_versions
                                                    │                 opportunity_events
                                                    └─▶ documents ──▶ download ──▶ sha256
                                                                       │
                                                                       ├─▶ storage (local/S3)
                                                                       └─▶ text extraction ─▶ OCR?
                                                                                    │
      services ──▶ Pydantic schemas ──▶ FastAPI routes ◀── search backend ◀─────────┘
                                          ▲
                                          │ api_access: scope check, then rate-limit budget
                                          │ (health probes and the OpenAPI document sit outside it)
```

## Layer rules

1. **Routes are thin.** A handler resolves dependencies, calls one service method and serialises
   the result. No SQL, no business rules, no giant handlers.
2. **Services own behaviour.** They take typed filter/query objects, return ORM objects or plain
   dataclasses, and raise domain errors (`NotFoundError`, `ValidationError`, ...).
3. **Models are never exposed.** SQLAlchemy objects stop at the service boundary; the API layer
   converts them with `model_validate`.
4. **Connectors never touch the database.** They receive an immutable `SourceContext` and emit
   `RawItem`s. Persistence is the pipeline's job.
5. **Failures are data.** A source that breaks produces `ingestion_errors` rows and a health
   downgrade, not an exception that kills the worker.
6. **Unknown beats invented.** Every stage prefers `NULL` and records a quality issue rather than
   guessing a value.

## Module map

| Package | Responsibility |
| --- | --- |
| `app/config.py` | Pydantic-settings configuration; validates production requirements |
| `app/logging.py` | structlog setup, request/source/job context vars, secret redaction |
| `app/errors.py` | Error hierarchy → HTTP status + stable error code, and the one response envelope reused by handlers and middleware |
| `app/api/query_filters.py` | Turns Pydantic's rejection of a query filter into the API's 422 envelope (see `docs/API.md`) |
| `app/enums.py` | Extensible string enums with tolerant `parse()`, typed as the concrete member |
| `app/db/` | `Base`, dialect-portable column types, models, session factory |
| `app/schemas/` | Pydantic v2 request/response contracts |
| `app/connectors/` | `ProcurementConnector` ABC, registry, built-in adapters |
| `app/ingestion/` | HTTP fetcher, parser driver, validator, normalizer, deduplicator, version engine, pipeline |
| `app/documents/` | Blob storage ABC, downloader, extractor, OCR hook, classifier |
| `app/search/` | Search backend selection (PostgreSQL FTS or portable LIKE) |
| `app/services/` | Tender, search, municipality, source, document and statistics services |
| `app/api/` | Middleware, exception handlers, dependencies, versioned router |
| `app/workers/` | ARQ queue, ingestion tasks, cron schedule |
| `app/ai/` | Optional enrichment provider; `NullAIProvider` by default |

## Request flow

1. `RequestContextMiddleware` assigns/echoes `X-Request-ID` and binds it to the log context.
2. FastAPI validates typed query parameters into a `TenderFilter` / `SearchQuery` /
   `PaginationParams` object; anything unexpected is a 422 with structured details.
3. A dependency provides an `AsyncSession` scoped to the request and a service built around it.
4. The service composes a SQLAlchemy statement, applies filters, a stable sort (always with a
   tiebreaker so pagination cannot repeat or skip rows) and `LIMIT/OFFSET`.
5. The route serialises to Pydantic schemas and wraps them in `ListResponse` / `DataResponse`.
6. Errors are converted by the registered exception handlers into the single error envelope.

## Ingestion flow

1. The scheduler selects sources that are `active` and due (`crawl_frequency_minutes`, backed off
   by consecutive failures) and enqueues one job per source.
2. `IngestionPipeline.run_source` creates a `source_runs` row and builds the connector from the
   registry by `connector_key` (falling back to a default per `connector_type`).
3. `parse_source` drives `discover → fetch → parse`, isolating per-target failures into
   `StageError`s so one broken listing page does not lose the rest.
4. Each `RawItem` is validated, normalized (dates, money, reference numbers, enums) and hashed.
5. The deduplicator resolves the record against what exists (four layers, see
   [INGESTION.md](INGESTION.md)).
6. New records are inserted with a creation version and event; changed records get a new version,
   field-level diff and semantic events; unchanged records are a no-op.
7. Documents referenced by the item are recorded as links; downloading and text extraction happen
   in a separate task so a slow PDF never blocks discovery.
8. `_finalise` writes counters, persists `ingestion_errors`, and derives the source's health.

## Storage and search abstractions

* `BlobStorage` is an ABC with a local filesystem implementation; the S3-compatible backend is the
  documented extension point. Documents are addressed by SHA-256, never by filename.
* `SearchBackend` is chosen per session dialect: PostgreSQL uses `tsvector`/`ts_rank` plus trigram
  similarity; other dialects fall back to a portable `LIKE` search. The API response contract is
  identical either way, so a dedicated search cluster can be introduced later without breaking
  clients.

## Extension points (deliberately not implemented)

* **Billing, quotas, subscriptions, user accounts** — nothing is implemented, and there are no
  placeholder tables to unpick. What *is* implemented is the part a data API needs before it can be
  exposed at all: API keys (`app/api/auth.py` + `app/services/api_key_service.py`; digests only,
  scopes, immediate revocation) and rate limiting (`app/services/rate_limit.py`; Redis fixed window with
  an in-process fallback that reports itself in `X-RateLimit-Policy`). The seam for a future billing
  tier is one function — `policy_for(tier, settings)` — not a schema change.
* **AI enrichment** — `AIProvider` ABC with a null implementation. Vendor adapters raise
  `NotImplementedError` rather than pretending to work.
* **Object storage** — swap `LocalBlobStorage` for an S3 implementation behind the same ABC.
