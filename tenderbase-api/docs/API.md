# API reference

Base URL: `/api/v1`  ·  Interactive docs: `/api/docs` · `/api/redoc` · Schema: `/openapi.json`

All responses are JSON. Every response carries `X-Request-ID`; send your own to correlate logs.

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
| `page` | int ≥ 1 | 1 | out of range → 422 |
| `page_size` | int 1–200 | 25 | server-enforced maximum; larger → 422 |

Ordering always includes a stable tiebreaker (`id`), so pages never overlap or skip rows.

A few endpoints return naturally bounded reference collections in full — `/provinces`,
`/provinces/districts`, `/sources/connectors`, `/categories`, `/tenders/{id}/documents`. They keep
the same envelope, with the `pagination` block describing the complete set, and they do not accept
`page`/`page_size`.

### Error codes

| HTTP | Code | Meaning |
| --- | --- | --- |
| 404 | `NOT_FOUND`, `TENDER_NOT_FOUND`, `SOURCE_NOT_FOUND`, `DOCUMENT_NOT_FOUND`, `MUNICIPALITY_NOT_FOUND`, `PROVINCE_NOT_FOUND`, `DOCUMENT_TEXT_NOT_FOUND` | Resource does not exist |
| 422 | `VALIDATION_ERROR` | Query/path validation failed; `details` lists each field |
| 429 | `RATE_LIMITED` | Only when rate limiting is enabled |
| 500 | `INTERNAL_ERROR` | Unexpected failure; message is generic, details go to the logs |
| 503 | `SERVICE_UNAVAILABLE`, `QUEUE_UNAVAILABLE` | A dependency (database, Redis) is down |

---

## Health

| Endpoint | Description |
| --- | --- |
| `GET /health` | Overall status plus component checks; 503 when a critical dependency fails |
| `GET /health/live` | Liveness — no I/O, always 200 while the process runs |
| `GET /health/ready` | Readiness — verifies the database is reachable |

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
`last_success_at: null`.

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
