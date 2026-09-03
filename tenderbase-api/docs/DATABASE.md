# Database

PostgreSQL 14+ in production. The schema is created and evolved exclusively through Alembic
migrations; models are the source of truth for autogeneration.

All tables use:

* **UUID primary keys** (`uuid4`, generated in Python so IDs are known before flush)
* **timezone-aware timestamps** stored in UTC (`created_at`, `updated_at` on every domain table)
* explicit **foreign keys with `ON DELETE` behaviour**, **unique constraints**, **check
  constraints** and indexes on every column the API filters or sorts by

Dialect portability: UUID columns are `UUID(as_uuid=True)` with a `String(36)` SQLite variant, and
JSON columns are `JSONB` with a `JSON` variant. This lets the whole test suite — including the
migration chain — run without a database server, while production still gets native types.

## Tables

17 domain tables (plus Alembic's `alembic_version`).

### Geography

| Table | Purpose | Key columns / constraints |
| --- | --- | --- |
| `provinces` | The 9 South African provinces | `code` unique (`EC, FS, GP, KZN, LP, MP, NC, NW, WC`), `slug`, `data_source` provenance |
| `districts` | District and metropolitan municipalities | `code` unique, `province_id` FK, `type` |
| `municipalities` | Metro / district / local municipalities | `code` unique, `type ∈ {METROPOLITAN, DISTRICT, LOCAL}`, `province_id`, `district_id`, `official_website`, `data_source` |

Provinces ship as bundled reference data with provenance (`data/geography/provinces.json`).
Municipalities are **not** bundled — they are imported by an operator from an authoritative CSV
(`scripts/import_geography.py --municipalities file.csv`) so the platform never carries invented
place names or websites.

### Sources

| Table | Purpose | Key columns / constraints |
| --- | --- | --- |
| `municipality_sources` | The source registry: one row per place data is collected from | `slug` unique; `base_url`; `source_type`; `procurement_scope`; `connector_type`, `connector_key`, JSON `config`; `active`, `priority`, `crawl_frequency_minutes`; health block (`health_status`, `last_run_at`, `last_success_at`, `last_failure_at`, `consecutive_failures`, `average_response_time_ms`, `last_http_status`); politeness (`robots_policy`, `rate_limit_per_minute`); lifecycle (`lifecycle_status`, `paused_at`, `paused_reason`); verification (`verification_status`, `verification_at`, `verification_http_status`, `verification_result` JSON with every check's status and evidence); `notes`, `verified_at` |
| `source_connectors` | Mirror of the in-process connector registry, so the API can describe capabilities | `key`, `name`, `connector_type`, `version`, `requires_browser`, `production_ready`, `status_note`, `config_schema`, `enabled` |
| `source_runs` | One row per execution of a source | `status`, `started_at`, `completed_at`, `duration_ms`, `items_found/created/updated/skipped/failed`, `documents_found`, `http_status`, `error_message` |

Two lifecycle/verification columns look redundant and are not. `active` is the *scheduling* switch
(only `ACTIVE`/`DEGRADED` sources are due for a run); `lifecycle_status` is the state machine an operator
drives (`DISCOVERED → PENDING_VERIFICATION → VERIFIED → ACTIVE → DEGRADED/PAUSED/DISABLED`). And
`verification_at` (when the automated procedure last observed evidence) is a different fact from
`verified_at` (when a human signed the source off) — no code ever sets the latter, which is what makes it
usable as a signature.

Check constraints keep operational values sane: `rate_limit_per_minute > 0`,
`crawl_frequency_minutes >= 5`, `0 <= priority <= 1000`, `items_found >= 0`, and on the registry
`lifecycle_status_known` / `verification_status_known` (the parsed-enum domains) plus
`passed_verification_is_dated`:

```sql
verification_status NOT IN ('PASSED','PASSED_WITH_WARNINGS') OR verification_at IS NOT NULL
```

so a source cannot claim a passing verification with no recorded observation. It deliberately says
nothing about `verified_at` — that column belongs to humans — and there is deliberately no constraint
tying `lifecycle_status='ACTIVE'` to a passing verification, because re-verifying a broken ACTIVE source
must be able to record `FAILED` while it stays active. Activation itself is guarded in
`VerificationService.set_lifecycle`, which refuses `ACTIVE` without a passing verification and `PAUSED`
without a reason.

### Opportunities (the spine)

`procurement_opportunities` — 42 columns, the canonical record:

| Group | Columns |
| --- | --- |
| Identity | `id`, `external_id`, `reference_number`, `reference_number_normalized` |
| Content | `title`, `description`, `procurement_type`, `status` |
| Issuer | `organization`, `municipality_id`, `province_id`, `source_id` |
| Dates | `published_at`, `closing_at`, `source_timezone`, `raw_dates` (JSON: exactly what the site said) |
| Money | `estimated_value`, `currency` |
| Submission | `submission_method`, `submission_url`, `submission_address` |
| Briefing | `briefing_required`, `briefing_compulsory`, `briefing_date`, `briefing_location` |
| Provenance | `source_url`, `canonical_url`, `content_hash`, `fingerprint`, `raw_payload_key`, `parser_metadata` |
| Quality | `data_quality`, `quality_issues` (JSON), `confidence` |
| Lifecycle | `version`, `first_seen_at`, `last_seen_at`, `is_test_fixture`, `created_at`, `updated_at` |

Constraints and indexes:

* `UNIQUE (municipality_id, reference_number)` — layer‑1 dedup key
* `UNIQUE (source_id, external_id)` — stable IDs from API sources (eTender OCIDs, WordPress IDs)
* `UNIQUE (fingerprint)` — layer‑3 identity; the pipeline resolves matches *before* insert
* `CHECK (estimated_value >= 0)`, `CHECK (version >= 1)`
* Indexes on `closing_at`, `published_at`, `status`, `procurement_type`, `municipality_id`,
  `province_id`, `source_id`, `content_hash`, `is_test_fixture` and `(status, closing_at)`

Related tables:

| Table | Purpose |
| --- | --- |
| `opportunity_versions` | Append-only history: `version`, `content_hash`, JSON `snapshot`, `changed_fields`, `source_run_id`, `observed_at`; unique `(opportunity_id, version)` |
| `opportunity_events` | Semantic change feed: `event_type` (`CREATED`, `DEADLINE_CHANGED`, `STATUS_CHANGED`, `BRIEFING_CHANGED`, `DOCUMENT_ADDED`, `DOCUMENT_REMOVED`, `AWARDED`, ...), `field`, `old_value`, `new_value`, `occurred_at` |
| `contacts` | Deduplicated contact people; `fingerprint` unique over name/email/phone/organization |
| `categories` | Taxonomy (slug unique, `parent_id`, `keywords`) |
| `opportunity_categories` | Many-to-many with `confidence` and `assigned_by` (`RULE`, `AI`, `MANUAL`) |

### Documents

| Table | Purpose | Key columns / constraints |
| --- | --- | --- |
| `documents` | One row per document link discovered on an opportunity | `UNIQUE (opportunity_id, source_url)`; `document_type`, `document_format`, `mime_type`, `file_size`, `sha256`, `storage_key`, `page_count`, `is_downloaded`, `download_error`, `current_version`; `CHECK (file_size >= 0)` |
| `document_versions` | Content history — a re-uploaded PDF is a new version, not a new document | `UNIQUE (document_id, sha256)`, `version`, `etag`, `last_modified`, `downloaded_at` |
| `document_text` | Extracted text and its provenance | `UNIQUE (document_id)`, `extraction_method`, `ocr_used`, `char_count`, `page_count`, `language`, `extraction_confidence`, `structure` |

Document identity is the **SHA-256 of the bytes**, never the filename: municipalities routinely
publish `document.pdf` a hundred times.

### Ingestion bookkeeping

| Table | Purpose |
| --- | --- |
| `ingestion_jobs` | Queued/running/completed work with attempt counters (`CHECK attempt >= 0`, `CHECK max_attempts > 0`), result JSON and timing |
| `ingestion_errors` | Per-stage failures: `stage` (`DISCOVERY`, `FETCH`, `PARSE`, `VALIDATE`, `NORMALIZE`, `PERSIST`, `DOCUMENT`, `UNKNOWN`), `error_type`, `message`, `url`, `context`, `is_retryable` |

### Security

| Table | Purpose | Key columns / constraints |
| --- | --- | --- |
| `api_keys` | Credentials for API consumers | `key_hash` unique — a keyed HMAC-SHA256 digest, never the key; `key_prefix` for recognition; `name`, `scopes` (JSON array), `status` (`ACTIVE`/`REVOKED`/`EXPIRED`), `expires_at`, `last_used_at`, `last_used_ip`, `created_by`, `notes`, `revoked_at`, `revoked_reason`; `CHECK (length(key_prefix) >= 6)` (a prefix
shorter than that is not identifying, it is noise), `status IN ('ACTIVE','REVOKED','EXPIRED')`, and
`revoked_keys_are_stamped` (`revoked_at IS NOT NULL OR status <> 'REVOKED'`) — a revocation that is not
timestamped cannot be audited, so it is refused at the database rather than by convention |

There is no usage counter, quota or tier column on purpose: those belong to a billing feature that this
build does not have, and adding nullable placeholders now would mean a migration plus dead code later.
Rate-limit policy comes from configuration (`policy_for(tier, settings)`), so changing limits requires
no schema change.

## Migrations

| Revision | Contents |
| --- | --- |
| `27f45e7c21d7` | Initial schema — all 17 tables, constraints and indexes; dialect-portable |
| `b2f1c9d40a11` | PostgreSQL-only search support: `pg_trgm`, a GIN full-text index over title/reference/description and trigram indexes; a no-op on other dialects |
| `c7a3d5e81b40` | Source lifecycle + verification columns, connector readiness flags (`production_ready`, `status_note`), and the `api_keys` table |
| `d9e4b7f205c3` | Corrects `passed_verification_is_dated` to require `verification_at` and to cover `PASSED_WITH_WARNINGS`; batch-compatible so SQLite rebuilds the table |

```bash
alembic upgrade head          # apply
alembic downgrade -1          # roll back one revision
alembic revision --autogenerate -m "describe change"
```

`migrations/env.py` reads the URL from application settings, so migrations can never run against a
hard-coded database; an explicit `sqlalchemy.url` (or `-x sqlalchemy.url=...`) overrides it, which
is how `tests/integration/test_migrations.py` exercises the chain on a throwaway SQLite file.

## Data-quality flags

`data_quality` is one of `VALID`, `INCOMPLETE`, `NEEDS_REVIEW`, `INVALID`. `quality_issues` holds a
JSON object explaining exactly why (missing closing date, unparsable value, duplicate needing
review, ...). Nothing is silently dropped and nothing is silently invented.
