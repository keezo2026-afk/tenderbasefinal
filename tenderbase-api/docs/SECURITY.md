# Security

TenderBase fetches URLs supplied by configuration and follows links found on third-party pages.
That makes the crawler the primary attack surface, and it is treated that way.

## Crawl ethics — hard rules

The platform ingests **only public information that is freely accessible without circumvention**.

It does **not**, and must never:

* bypass authentication, paywalls or access controls;
* solve, outsource or evade CAPTCHAs;
* defeat anti-bot systems, rotate residential proxies, or spoof a human browser to evade blocking;
* ignore `robots.txt` (`HTTP_RESPECT_ROBOTS=true` by default; a per-source `robots_policy` may only
  be relaxed by an operator with a documented, lawful reason);
* hammer a site — every host is rate-limited (`rate_limit_per_minute`, default 30/min) with jitter,
  and failures back off rather than retrying harder.

The user agent identifies the crawler and carries a contact address. If a site blocks us, the
correct response is to stop and mark the source, not to work around the control.

## SSRF and URL handling

Every outbound URL — from configuration, from a link on a page, and again after each redirect —
passes `app/utils/urls.validate_url`:

| Check | Behaviour |
| --- | --- |
| Scheme allowlist | Only `http`/`https`; `file:`, `ftp:`, `gopher:`, `data:`, `javascript:` rejected |
| Credentials in URL | `user:pass@host` rejected |
| Host resolution | Every resolved address inspected |
| Private/loopback/link-local/multicast/reserved ranges | Rejected unless `HTTP_ALLOW_PRIVATE_NETWORKS=true` (tests only) |
| Cloud metadata endpoints | `169.254.169.254` and friends explicitly blocked |
| Redirects | Followed at most `HTTP_MAX_REDIRECTS` (default 5); **the final URL is re-validated** |
| Response size | Streamed with a hard cap (`HTTP_MAX_RESPONSE_BYTES`, default 25 MiB) — no unbounded reads |
| Timeouts | Connect/read/write/pool timeouts on every request |
| Content type | Connectors declare what they accept; unexpected types are a parse error, not a silent success |

Blob storage keys are validated and path traversal (`../`) is rejected on read, write and delete.

## Secrets

* No credentials, API keys, database URLs or tokens are committed. `.env` is git-ignored; only
  `.env.example` with placeholders ships.
* Production startup **fails** if `SECRET_KEY` is still the placeholder or `DEBUG=true`.
* The structlog pipeline redacts keys matching `password`, `secret`, `token`, `api_key`,
  `authorization` and similar, and scrubs `password=`/`token=` patterns inside messages.
* Error responses never include stack traces, SQL, or internal paths — only a stable code, a safe
  message and the `request_id` needed to find the detail in the logs.
* AI credentials are optional; the core API starts and serves every endpoint without them.

## Input handling

* Every query and path parameter is validated by a typed Pydantic model; unknown parameters and
  out-of-range values are 422s with structured details.
* `page_size` has a server-enforced ceiling, so no client can request an unbounded result set.
* All database access goes through SQLAlchemy expressions with bound parameters. There is no
  string-built SQL; sort keys are matched against an allowlist rather than interpolated.
* The API is read-only: it exposes no write endpoints, so there is no mass-assignment surface.

## Data handling

* Contact details published in tender adverts are stored because they are part of the public
  record. They are exposed exactly as published, never enriched, cross-referenced or scored.
* Development fixtures are flagged `is_test_fixture=true`, titled `TEST FIXTURE`, restricted to
  `example.org`, excluded from API results by default and counted separately in statistics.
  `scripts/load_dev_fixtures.py` refuses to run when `APP_ENV=production`.
* Raw payloads are retained for reproducibility; treat blob storage as containing third-party
  content and apply your own retention policy.

## Not implemented (deliberately)

This build ships **no authentication, no API keys, no rate limiting and no billing**. If you expose
it publicly, put it behind a gateway that provides them. The extension points are prepared:

* `Settings.rate_limit_enabled` / `rate_limit_anonymous_per_minute` and a middleware hook;
* a clean place for an API-key dependency in `app/api/dependencies.py`;
* no user, subscription or invoice tables to unpick later.

## Dependency and supply chain

* Dependencies are pinned by lower bound in `pyproject.toml`; pin exactly in your deployment
  lockfile.
* The container runs as a non-root user, contains no build toolchain in the final stage, and
  installs no packages at runtime.
* Playwright and OCR are optional extras — the base image has neither a browser nor Tesseract, so
  the default attack surface stays small.

## Reporting

Report a suspected vulnerability privately to the repository owner with reproduction steps. Please
do not open a public issue containing an exploit, and do not test against third-party municipal
websites.
