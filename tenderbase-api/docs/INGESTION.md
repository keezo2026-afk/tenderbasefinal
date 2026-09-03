# Ingestion

One pipeline, one source at a time, always ending in a persisted `source_runs` row — success or
failure.

```
discover ─▶ fetch ─▶ parse ─▶ validate ─▶ normalize ─▶ deduplicate ─▶ version ─▶ persist ─▶ finalise
```

## Stages

| Stage | Module | What it does | On failure |
| --- | --- | --- | --- |
| Discover | connector + `ingestion/discovery.py` | Turns source config into `DiscoveryTarget`s | `StageError(DISCOVERY)`; run fails cleanly |
| Fetch | `ingestion/fetcher.py` | SSRF-validated HTTP with timeouts, redirect and size caps, retries with exponential backoff + jitter, per-host rate limiting, robots.txt | Retryable statuses retried; permanent errors recorded per target |
| Parse | connector `parse()` | HTML/JSON/PDF → `RawItem` (strings exactly as published) | `StageError(PARSE)`; other items survive |
| Validate | `ingestion/validator.py` | Required fields, plausibility (dates in a sane range, non-negative money, URL shape) | Sets `data_quality` and `quality_issues`; only truly unusable items are rejected |
| Normalize | `ingestion/normalizer.py` | Dates (SA formats, `Africa/Johannesburg` → UTC, `raw_dates` preserved), money (`R 1 250 000,00` → `Decimal`), reference numbers, enums, whitespace/entities | Unparsable → `NULL` + quality issue. Never a guess |
| Deduplicate | `ingestion/deduplicator.py` | Four layers, see below | Uncertain → stored separately for review |
| Version | `ingestion/versioning.py` | Field diff → new `opportunity_versions` row + semantic `opportunity_events` | — |
| Persist | `ingestion/pipeline.py` | Insert/update, documents, contacts, raw payload offload | Per-item `try/except` → `items_failed` |
| Finalise | `ingestion/pipeline.py` | Counters, `ingestion_errors`, source health, timing | Always runs |

Per-item isolation is absolute: one malformed row cannot fail a run, and one broken source cannot
fail the worker.

## Normalization rules

* **Dates** — `2026-09-15`, `15/09/2026`, `15 September 2026`, `15th Sep 2026 at 11:00`,
  `September 15, 2026`, and "closing date to be confirmed" (→ `NULL`). Naive datetimes are
  interpreted in the source's timezone (default `Africa/Johannesburg`) and stored in UTC. The
  original strings stay in `raw_dates`.
* **Money** — handles `R`, `ZAR`, thousands separators (space, comma, non-breaking space) and
  decimal commas. `estimated_value` is a `Decimal`; a value that cannot be parsed is `NULL`, never
  `0`.
* **Reference numbers** — stored as published *and* normalized (upper-cased, punctuation folded)
  for matching.
* **Enums** — publisher wording is mapped where it is unambiguous (`quotation` → `RFQ`,
  `bid`/`tender` → `TENDER`, `cancelled` → `CANCELLED`); anything else becomes `UNKNOWN`/`OTHER`.
* **Text** — HTML entities decoded, whitespace collapsed, boilerplate trimmed. Content is never
  rewritten or summarised at this stage.

## Deduplication

Layered, confidence-aware, and it **never auto-merges an uncertain match**.

| Layer | Key | Decision |
| --- | --- | --- |
| 1 | `(municipality_id, reference_number_normalized)` and `(source_id, external_id)` | `EXACT_MATCH` (confidence 1.0) |
| 2 | `content_hash` (canonical field set, order-independent) | `EXACT_MATCH` |
| 3 | `fingerprint` = hash of title + organization + closing date + type | `EXACT_MATCH` / `PROBABLE_MATCH` |
| 4 | PostgreSQL `pg_trgm` title similarity within a ±3-day closing window | ≥0.82 `PROBABLE_MATCH`; ≥0.65 `UNCERTAIN`; below → `NEW` |

* `EXACT_MATCH` / `PROBABLE_MATCH` → update the existing record (versioned).
* `UNCERTAIN` → **insert as a separate record**, flag `data_quality = NEEDS_REVIEW` and record
  `quality_issues.duplicate_review` with the candidate ID. A human decides.
* `NEW` → insert.
* Layer 4 is PostgreSQL-only; on other dialects it degrades to "no match" rather than pretending.

The same reference number issued by two different municipalities is **not** a duplicate — layer 1
is always scoped by issuer.

## Versioning and events

* Creation writes version 1 plus a `CREATED` event.
* On change, the version engine diffs the canonical fields. A `None` incoming value **never**
  overwrites a known stored value — losing data to a flaky parse is worse than a stale field.
* A new `opportunity_versions` row stores the full snapshot, the changed-field list and the run ID.
* Semantic events are emitted for the changes that matter: `DEADLINE_CHANGED`, `STATUS_CHANGED`,
  `BRIEFING_CHANGED`, `DOCUMENT_ADDED`, `AWARDED`, ...
* Cosmetic-only differences refresh `content_hash` without creating a version.
* A document that disappears from the page is informational (`DOCUMENT_REMOVED` event) and does not
  create a new version — sites reshuffle links constantly.

## Documents

Discovery records links only (`is_downloaded = false`). Downloading, hashing, storing and text
extraction run as separate tasks — see [DOCUMENTS.md](DOCUMENTS.md).

## Raw data preservation

Every item's original payload is kept: small payloads inline in `parser_metadata`/`raw_payload`,
larger ones offloaded to blob storage with the key in `raw_payload_key`. Re-parsing history is
therefore always possible after a parser fix.

## Scheduling, retries and health

* Sources are due when `last_run_at + crawl_frequency_minutes` has passed, ordered by `priority`.
* Backoff multiplies the interval by consecutive failures: ×1 (0), ×2 (1–3), ×4 (4–9), ×12 (10+).
  A failing source is slowed down, never silently disabled.
* HTTP retries: bounded attempts with exponential backoff **and jitter**, only for retryable
  statuses (408/425/429/5xx) and transport errors.
* Health after each run:

| Consecutive failures | Health |
| --- | --- |
| 0 | `HEALTHY` |
| 1–3 | `DEGRADED` |
| 4–9 | `FAILING` |
| 10+ | `OFFLINE` |

A source that has never run is `UNKNOWN`. Health is always derived from real runs.

## Source verification

Before a source is activated, `app/ingestion/verifier.py` answers one question with evidence: **can
this configuration be turned into records?** "The URL returned 200" is not an answer — a portal that
serves a friendly maintenance page, a search-results template with no rows, or a page whose links all
point at another domain all return 200.

| Check | Required | What it proves |
| --- | --- | --- |
| `url` | yes | DNS resolves and the SSRF policy (scheme, credentials, address ranges, **port**) accepts the URL |
| `access` | yes | The listing URL answers with a success status |
| `connector` | yes | The configured connector key exists, accepts the config keys given, and — for `browser.playwright` — that Playwright is actually installed |
| `listing` | yes | The connector discovered URLs to fetch from the configured paths |
| `parse` | yes | A fetched listing page yields structurally valid rows (`item_selector` matched something) |
| `parser` | yes | Items normalize and validate — they are usable, not merely present |
| `robots` | no | Our user-agent is permitted to fetch the paths we need |
| `detail` | no | Linked detail pages are fetchable and non-empty |
| `documents` | no | Document links were found on the sampled items |
| `pagination` | no | A second page is reachable and the sequence terminates |

Weight is a property of the check, never of its outcome: `CheckResult.required` is derived from
`REQUIRED_CHECKS`/`OPTIONAL_CHECKS` by name, so a check cannot be blocking in one report and optional in
another. Each check records `PASSED`/`WARNING`/`FAILED`/`SKIPPED` **plus structured evidence** (HTTP status,
selectors that matched, counts, the failing exception's class), stored in
`municipality_sources.verification_result`. `SKIPPED` is never counted as success.

Warnings are things an operator should know but which do not make a source unusable: `robots.txt` says no
while we are configured to ignore it, discovered links point at a different domain, a source configured
for pagination that only ever has one page. Failures that are *not* warnings: an access-controlled page
(401/403 — reported as "we do not bypass authentication", with `transient: false`, so it is never retried
as if it were a network blip), a listing that yields no rows, items that fail validation.

Verification never activates anything. A passing run moves `DISCOVERED`/`PENDING_VERIFICATION` to
`VERIFIED`; `ACTIVE` requires a separate, human decision (`--activate --reason "..."`), which is
refused outright without a passing verification. `verified_at` — the human sign-off — is never written by
code.

```bash
python -m scripts.verify_source <id-or-slug> --no-store   # nothing persisted
python -m scripts.verify_source --discover                # every unverified source
python -m scripts.verify_source <id> --json               # machine-readable, for CI or a dashboard
```

In an environment with no route to the public internet, every source reports `FAILED` for the network
checks. That is the correct output, not a bug to work around: TenderBase records what it observed.

## Running ingestion

```bash
# one source, no writes — prints discovered targets and the first 10 parsed items
python -m scripts.run_ingestion --slug my-source --dry-run

python -m scripts.run_ingestion --slug my-source
python -m scripts.run_ingestion --source-id <uuid>
python -m scripts.run_ingestion --due --limit 20        # everything currently due
```

Background execution (Redis + ARQ):

```bash
arq app.workers.scheduler.WorkerSettings
```

The worker runs the cron schedule (enqueue due sources, process pending documents, refresh
statistics). If Redis is unavailable the API still serves reads and enqueue attempts fail loudly
with `QUEUE_UNAVAILABLE` rather than silently dropping work.

## Observability

Every log line carries `request_id`, `source_id` and `job_id` where applicable. Key events:
`pipeline.start`, `pipeline.item_failed`, `pipeline.finished`, `fetch.ok`, `fetch.retry`,
`parser.stage_failed`, `document.extraction_failed`. Errors are also persisted in
`ingestion_errors` so operators can query them through the database rather than grepping logs.
