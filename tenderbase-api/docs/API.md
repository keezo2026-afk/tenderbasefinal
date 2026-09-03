# API reference

Base URL: `/api/v1`  ·  Interactive docs: `/api/docs` · `/api/redoc` · Schema: `/openapi.json`

All responses are JSON. Every response carries `X-Request-ID`; send your own to correlate logs.

## Authentication

```bash
curl -H "X-API-Key: $TENDERBASE_KEY" https://api.tenderbase.example/api/v1/tenders?status=open
```

Every endpoint under `/api/v1/` that returns data requires a valid key with the scope for that path,
except the health probes, the documentation routes and `/openapi.json`. `Authorization: Bearer <key>`
is accepted for clients that cannot send a custom header. Keys are issued by operators:

```bash
python -m scripts.manage_api_keys create --name "Partner X" --scopes read:tenders --expires-days 180

# Scopes may be space- or comma-separated, and a preset expands to its members.
python -m scripts.manage_api_keys create --name "Partner X" \
    --scopes read:tenders,read:statistics --expires-days 30
```

| Scope | Unlocks |
| --- | --- |
| `read:tenders` | `/tenders`, `/search`, `/events`, `/municipalities` (including their documents/events sub-resources) |
| `read:geography` | `/provinces`, `/categories` |
| `read:documents` | `/documents` |
| `read:sources` | `/sources`, `/operations` |
| `read:statistics` | `/statistics` |
| `admin` | `/api-keys` (list, mint, revoke) and every read scope |

This table is enforced rather than merely documented:
`tests/integration/test_api_scope_table.py` compares it with the two in-code tables
(`ROUTER_SCOPES`, which is the router dependency that actually gates a request, and
`SCOPE_REQUIREMENTS`, the path fallback), checks that every documented `/api/v1` route falls under a
family with a declared scope, and then drives real requests per family with a real key — asserting both
that a wrong scope is refused with the right message and that the right scope is let through. A route
added without a scope, or a scope table edited in only one place, fails that module.

A missing/invalid/expired/revoked key is `401`; a valid key without the scope is `403` naming the scope
it needed. Enforcement defaults to on in `production`/`staging` and cannot be disabled there
(`API_KEY_ENFORCEMENT_ENABLED`). See `docs/SECURITY.md` for how keys are stored and revoked.

### Rate limiting

When `RATE_LIMIT_ENABLED=true`, every response also carries the budget:

```
X-RateLimit-Limit: 60          X-RateLimit-Remaining: 58
X-RateLimit-Reset: 1756809300  X-RateLimit-Policy: redis
```

`X-RateLimit-Policy` is `redis`, `in-process` or `in-process (redis unavailable)` — the last one means
limits are being enforced per replica rather than globally, so a client that needs exact quota
behaviour should treat it as a server-side incident. Refusals are `429` with `Retry-After` in seconds
and the same envelope as every other error.

## Envelopes

**List**

```json
{
  "data": [ { "...": "..." } ],
  "pagination": {
    "page": 1, "page_size": 25, "total_items": 143,
    "total_pages": 6, "has_next": true, "has_previous": false
  },
  "meta": { "request_id": "6f1e...", "generated_at": "2026-09-02T10:15:00Z", "extra": null }
}
```

**Single**

```json
{ "data": { "...": "..." }, "meta": { "request_id": "6f1e...", "generated_at": "..." } }
```

**Error**

```json
{ "error": { "code": "TENDER_NOT_FOUND", "message": "Opportunity not found",
             "request_id": "6f1e...", "details": {} } }
```

### Pagination

| Parameter | Type | Default | Rules |
| --- | --- | --- | --- |
| `page` | int 1–10 000 | 1 | out of range → 422 |
| `page_size` | int ≥ 1 | `DEFAULT_PAGE_SIZE` (25) | bounded by `MAX_PAGE_SIZE` (100); larger → 422 |

`page_size` is refused rather than clamped. Silently answering a request for 80 rows with 50 makes the
`pagination` block disagree with what the client asked for, which is worse than an error it can act on —
the maximum is an operator setting (`MAX_PAGE_SIZE`), so a client learns the ceiling from the message.

**Validation errors** name every offending parameter, whichever layer caught it:

```json
{ "error": { "code": "VALIDATION_ERROR", "message": "One or more query parameters are invalid",
  "request_id": "6f1e...",
  "details": { "errors": [ { "field": "query.status",
    "message": "Input should be 'DRAFT', 'PUBLISHED', …", "type": "enum" } ] } } }
```

`field` is the location prefix plus the parameter as the client wrote it (`query.page_size`,
`query.published_after.date` for a value that is neither date nor datetime). A check that spans two
parameters — `published_after` after `published_before` — has no single field to blame and reports the
bare `query` with the rule in the message.

Filter values are matched exactly and case-sensitively (`OPEN`, not `open`). In a query string `+` means
a space, so a timezone offset must be percent-encoded: `?closing_after=2026-09-01T08:30:00%2B02:00`,
or use `Z` for UTC.

Ordering always includes a stable tiebreaker (`id`), so pages never overlap or skip rows.

A few endpoints return naturally bounded reference collections in full — `/provinces`,
`/provinces/districts`, `/sources/connectors`, `/categories`, `/tenders/{id}/documents`. They keep
the same envelope, with the `pagination` block describing the complete set, and they do not accept
`page`/`page_size`.

### Error codes

| HTTP | Code | Meaning |
| --- | --- | --- |
| 401 | `API_KEY_MISSING`, `API_KEY_INVALID` | No credential, or one that is not accepted. Deliberately indistinguishable |
| 403 | `INSUFFICIENT_SCOPE`, `FORBIDDEN` | Valid key, wrong scope — `message` names the scope required. Also returned when key minting is attempted with `API_KEY_SELF_SERVICE_ENABLED=false` |
| 404 | `NOT_FOUND`, `TENDER_NOT_FOUND`, `SOURCE_NOT_FOUND`, `DOCUMENT_NOT_FOUND`, `MUNICIPALITY_NOT_FOUND`, `PROVINCE_NOT_FOUND`, `DOCUMENT_TEXT_NOT_FOUND` | Resource does not exist |
| 422 | `VALIDATION_ERROR`, `SOURCE_NOT_VERIFIED`, `REASON_REQUIRED` | Query/path validation failed (`details` lists each field), or a source lifecycle change was refused by the guard rails in `VerificationService.set_lifecycle` |
| 429 | `RATE_LIMITED` | Only when rate limiting is enabled; `Retry-After` is set |
| 500 | `INTERNAL_ERROR` | Unexpected failure; message is generic, details go to the logs |
| 503 | `SERVICE_UNAVAILABLE`, `QUEUE_UNAVAILABLE`, `RATE_LIMIT_BACKEND_UNAVAILABLE` | A dependency (database, Redis) is down; the last only with `RATE_LIMIT_FAIL_OPEN=false` |

---

## Health

| Endpoint | Description |
| --- | --- |
| `GET /health` | Every dependency with latency. 503 only when a *required* one fails; an optional one is reported as `degraded` and keeps answering 200 |
| `GET /health/live` | Liveness — no I/O, always 200 while the process runs. Never depends on Redis |
| `GET /health/ready` | Readiness — the database must answer. Redis is required only when `RATE_LIMIT_FAIL_OPEN=false` |
| `GET /metrics` | Prometheus text exposition. Not in this schema; optionally gated on `METRICS_TOKEN` |

Each probe is also mounted at the application root (`/health`, `/health/live`, `/health/ready`) for
orchestrators and the container `HEALTHCHECK` that should not have to know the API version.

---

## Tenders

### `GET /tenders`

Paginated, filterable list of canonical opportunities.

| Parameter | Type | Notes |
| --- | --- | --- |
| `province` | string | Name, code or slug |
| `district` | string | Name, code or slug |
| `municipality` | string | Name, code or slug |
| `municipality_id`, `source_id` | UUID | Exact |
| `type` | enum | `RFQ`, `TENDER`, `RFP`, `EOI`, `RFI`, `AUCTION`, `OTHER`, `UNKNOWN` |
| `status` | enum | `OPEN`, `CLOSED`, `AWARDED`, `CANCELLED`, `EXTENDED`, `UNKNOWN` |
| `category` | string | Category slug |
| `reference_number` | string | Normalized before matching |
| `published_after` / `published_before` | date/datetime | ISO 8601 |
| `closing_after` / `closing_before` | date/datetime | ISO 8601 |
| `min_value` / `max_value` | number ≥ 0 | `min > max` → 422 |
| `data_quality` | enum | `VALID`, `INCOMPLETE`, `NEEDS_REVIEW`, `INVALID` |
| `include_test_fixtures` | bool | Default `false` — development fixtures are hidden |
| `q` | string | Simple text filter (use `/search` for ranked search) |
| `sort` | string | `published_at`, `closing_at`, `created_at`, `last_seen_at`, `title`, `relevance`; prefix `-` for descending. Default `-published_at` |

Example:

```
GET /api/v1/tenders?province=KZN&type=RFQ&closing_after=2026-09-01&sort=closing_at&page_size=50
```

### `GET /tenders/{tender_id}`

Full record: content, dates (plus `raw_dates` as published), submission and briefing details,
contact, `documents[]`, `categories[]`, provenance (`source_url`, `content_hash`, `source`) and
data-quality assessment.

### `GET /tenders/{tender_id}/documents`

Document metadata. `is_downloaded=false` means only the link has been discovered; `sha256` and
`file_size` appear after download.

### `GET /tenders/{tender_id}/events`

Chronological change feed (newest first): `DEADLINE_CHANGED`, `STATUS_CHANGED`,
`BRIEFING_CHANGED`, `DOCUMENT_ADDED`, `DOCUMENT_REMOVED`, `AWARDED`, `CREATED`, each with
`field`, `old_value`, `new_value`, `occurred_at`.

### `GET /tenders/{tender_id}/versions`

Immutable version history with `content_hash` and the field-level diff against the prior version.

---

## Search

### `GET /search`

| Parameter | Type | Notes |
| --- | --- | --- |
| `q` | string, 2–300 chars | **Required** |
| `sort` | string | `relevance` (default), `closing_at`, `-published_at`, ... |
| plus every `/tenders` filter | | |

Each hit adds `score` and `snippet`. On PostgreSQL these come from `ts_rank` and `ts_headline`; on
other dialects the fallback returns `score: null` and no snippet — the response *shape* never
changes. `meta.extra` reports `query`, `search_backend` and `took_ms`.

---

## Geography

| Endpoint | Description |
| --- | --- |
| `GET /provinces` | All provinces, with municipality counts |
| `GET /provinces/districts` | Districts, filterable by `province` |
| `GET /provinces/{identifier}` | By UUID, code (`KZN`) or slug |
| `GET /municipalities` | Filter by `province`, `district`, `type`, `has_sources`, `q` |
| `GET /municipalities/{identifier}` | By UUID, code or slug |
| `GET /municipalities/{identifier}/tenders` | Opportunities for one municipality (same filters as `/tenders`) |

---

## Sources

| Endpoint | Description |
| --- | --- |
| `GET /sources` | The registry: connector, crawl policy, `health` block. Filters: `source_type`, `connector_type`, `health_status`, `province`, `municipality_id`, `active`, `q` |
| `GET /sources/connectors` | Connector implementations in this build and the config keys each accepts |
| `GET /sources/{source_id}` | One source, including operator `notes` and `verified_at` |
| `GET /sources/{source_id}/runs` | Execution history, newest first, with per-run counters |

Health is reported, never invented: a source that has not run yet is `UNKNOWN` with
`last_success_at: null`. `GET /sources/{id}` also exposes `verification_status`, `verification_at`
(automated, evidence-backed) and `verified_at` (a human signed it off) — two columns because those are
two different facts, and `verified_at` is never set by code.

`GET /sources/connectors` reports `production_ready` and `status_note` straight from the connector
registry, so a connector that needs Playwright, or one whose upstream contract has not been verified,
says so instead of looking implementable.

### Operations

Operator-facing reads (scope `read:sources`) over the same tables the API is built on. They change
nothing; `POST /operations/reconcile` is the single exception and requires `admin`:

| Endpoint | Description |
| --- | --- |
| `GET /operations/sources/freshness` | Per-source staleness: `FRESH` / `AGING` / `STALE` / `NEVER_RUN` / `PAUSED` / `NOT_ACTIVE`, worst first, optionally filtered by `?state=` |
| `POST /operations/reconcile` | Run the job/run/lease repair pass now (`?dry_run=true` to look first) |
| `GET /operations/sources/{id}/report` | One run in full: counters, per-stage timings, errors |
| `GET /operations/sources/{id}/history` | Recent runs with outcomes |
| `GET /operations/sources/{id}/verification` | The stored verification report: every check, its status and its evidence |
| `GET /operations/runs/failed` | Runs across all sources that failed, newest first |
| `GET /operations/sources/unhealthy` | `DEGRADED`/`FAILING`/`OFFLINE` sources with consecutive-failure counts |
| `GET /operations/duplicates/review` | Probable matches held for human review — never auto-merged |

### `GET /operations/sources/freshness`

Answers "is ingestion actually delivering, or merely running?" without a database session. Every row
carries `source_id`, `slug`, `name`, `active`, `lifecycle_status`, `freshness_state`, `last_run_at`,
`last_success_at`, `hours_since_success`, `next_run_at`, `claim_expires_at`, `health_status` and
`consecutive_failures`. Ordering is by state severity first (a stale active source before a paused one),
then by hours since last success, descending; `LIMIT` applies before filtering by `?state=`, so
an unfiltered page and a filtered one can disagree — narrow with `limit=` if that matters.

Timestamps are echoed exactly as stored, in UTC ISO-8601 (`+00:00`), and `hours_since_success` is
computed from the row's own stored value rather than re-derived from a second clock reading.
`PAUSED` and `NOT_ACTIVE` are reported instead of being dropped, because "we know this source is off" is
a different fact from "this source has gone quiet on its own". Thresholds come from
`FRESHNESS_AGING_HOURS` (36) and `FRESHNESS_STALE_HOURS` (96); `AGING` is inclusive at its threshold.
An unknown `state` is a `422` with `details.errors[].field == "query.state"`.

### `POST /operations/reconcile`

The same pass the worker runs every `JOB_RECONCILIATION_INTERVAL_SECONDS`, on demand. It compares
`ingestion_jobs` and `source_runs` against reality and repairs what a lost worker, a dropped queue or a
failed `finally` block leaves behind: a stuck `SourceRun` is closed `FAILED` with its counters and
duration preserved, a live job that stopped moving is re-dispatched (or failed, if
`RECONCILE_REENQUEUE=false`) with its source's backoff advanced and its next run made due, an expired
claim lease is cleared, and a duplicate live claim is cancelled. It never breaks a lease a worker still
holds and never resurrects a terminally failed job.

| Field | Meaning |
| --- | --- |
| `started_at` | When the pass began, UTC |
| `dry_run` | Echoes the query flag; with `true` nothing was written |
| `reenqueue_enabled` | The configured `RECONCILE_REENQUEUE` value, so a reader knows which policy produced the numbers |
| `actions_count` | Rows changed |
| `counts` | Per action: `requeued`, `stale_job_failed`, `source_run_closed`, `lease_expired_cleared`, `duplicate_job_cancelled` |
| `checked` | How many rows each stage examined, including `reenqueue_unavailable` when the queue could not be reached |
| `source_freshness` | Bucket totals, matching `GET /operations/sources/freshness` |
| `actions` | One entry per repair: `action`, `job_id`, `source_id`, `detail` |

**Idempotent by construction** — every repair moves the row out of the state that selected it, so a
second call in a row returns `actions_count=0`. Safe to re-run, and safe to point a monitoring check at.
The audit trail is one structured log line (`operations.reconcile`, with the acting key), because this
is operational bookkeeping rather than tender data.

### API keys

Requires `admin`, and `POST` requires `API_KEY_SELF_SERVICE_ENABLED=true` (default false: minting keys
is an audited operator action via `scripts/manage_api_keys.py`).

| Endpoint | Description |
| --- | --- |
| `GET /api-keys` | Keys with prefix, name, scopes, status and expiry. The secret is never listed |
| `POST /api-keys` | Mint a key. The response is the only place the raw value appears, and it is sent with `Cache-Control: no-store` |
| `GET /api-keys/{key_id}` | One key |
| `POST /api-keys/{key_id}/revoke` | Revoke now, with an optional `reason`; takes effect on the next request |
| `GET /api-keys/summary` | `active` / `revoked` / `expired` counts, for a dashboard

---

## Documents

| Endpoint | Description |
| --- | --- |
| `GET /documents` | Filter by `opportunity_id`, `document_type`, `document_format`, `is_downloaded`, `has_text` |
| `GET /documents/{document_id}` | Metadata including `sha256`, `file_size`, `page_count` |
| `GET /documents/{document_id}/versions` | Content history by hash |
| `GET /documents/{document_id}/text` | Extracted text plus provenance (`extraction_method`, `ocr_used`, `extraction_confidence`). `include_content=false` returns metadata only. 404 when nothing has been extracted — never a fabricated body |

The API serves document **metadata and text**, not file bytes; binaries live in blob storage and
are served (if at all) by the deployment's own storage layer.

---

## Reference

| Endpoint | Description |
| --- | --- |
| `GET /categories` | Taxonomy with opportunity counts |
| `GET /events` | Platform-wide change feed, filterable by `event_type`, `since`, `municipality` |
| `GET /statistics` | Aggregates computed from real ingested data only: totals, open opportunities, closing in 7 days, document and source counts, breakdowns by province/type/status/source health, and `test_fixture_opportunities` reported separately |

---

## Conventions

* All timestamps are ISO 8601 in **UTC**; `source_timezone` records what the publisher used, and
  `raw_dates` preserves the original strings.
* Money is a decimal with an explicit `currency` (usually `ZAR`); an unparsable amount is `null`,
  never `0`.
* Enum fields are strings and forward-compatible: unrecognised publisher values map to `UNKNOWN`
  or `OTHER` rather than failing the request.
* There is no authentication in this build. API keys and rate limiting are prepared extension
  points (`Settings.rate_limit_enabled`), deliberately not implemented.
