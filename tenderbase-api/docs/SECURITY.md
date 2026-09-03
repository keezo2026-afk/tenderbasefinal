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
| Port allowlist | `HTTP_ALLOWED_PORTS` (default `80,443,8080,8443`); anything else is refused |
| Redirects | Followed at most `HTTP_MAX_REDIRECTS` (default 5); **the final URL is re-validated** |
| Response size | Streamed with a hard cap (`HTTP_MAX_RESPONSE_BYTES`, default 25 MiB) — no unbounded reads |
| Timeouts | Connect/read/write/pool timeouts on every request |
| Content type | Connectors declare what they accept; unexpected types are a parse error, not a silent success |

### Why ports are on the list

An internal service reachable over a weird port is the classic payoff for an SSRF bug, and a listing
page that suddenly points at `http://169.254.169.254:9200/` is not how a municipal tender portal
behaves. So the port set is checked *before* the address, and the same configured set is used at every
judgement point — the initial URL, each redirect hop, document streaming, the verification probe and
source registration — because a check the fetcher applies but the verifier does not is no check.

8080/8443 are in the default set because South African government portals genuinely run on them (legacy
SAP Portal deployments); they are not a loose allowance. Widening is a deliberate act:

```bash
HTTP_ALLOWED_PORTS=80,443,8080,8443,8008   # and record why in the deployment notes
```

The verification report shows a refused URL as a `url` check failure with the port in the evidence, so a
source blocked by policy looks different from one that is genuinely offline — which is the point.

Blob storage keys are validated and path traversal (`../`) is rejected on read, write and delete.

## Secrets

* No credentials, API keys, database URLs or tokens are committed. `.env` is git-ignored; only
  `.env.example` with placeholders ships — and a test asserts that file lists exactly the variables
  `Settings` reads, with the same defaults, so the documented surface cannot drift into fiction.
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
* The API is read-only for procurement data: it exposes no write endpoints for opportunities,
  sources or documents, so there is no mass-assignment surface there. The two mutating endpoints
  (`POST /api/v1/api-keys`, `POST /api/v1/api-keys/{id}/revoke`) require the `admin` scope, are
  disabled by default, and accept a typed model with a 300-character name and an enumerated scope list.

## Data handling

* Contact details published in tender adverts are stored because they are part of the public
  record. They are exposed exactly as published, never enriched, cross-referenced or scored.
* Development fixtures are flagged `is_test_fixture=true`, titled `TEST FIXTURE`, restricted to
  `example.org`, excluded from API results by default and counted separately in statistics.
  `scripts/load_dev_fixtures.py` refuses to run when `APP_ENV=production`.
* Raw payloads are retained for reproducibility; treat blob storage as containing third-party
  content and apply your own retention policy.

## API authentication

Every data endpoint under `/api/v1/` requires an API key when enforcement is on — which it is, by
default, in `production` and `staging`, and cannot be switched off there. Health probes, `/api/docs`
and `/openapi.json` stay public: an orchestrator cannot hold a credential, and a probe that fails
because of authentication is an outage of the wrong thing.

* **Transport.** `X-API-Key: tb_live_...`, or `Authorization: Bearer tb_live_...` for clients that can
  only send bearer tokens. Both are checked identically.
* **Storage.** Only a keyed HMAC-SHA256 digest is stored (`API_KEY_PEPPER`, falling back to
  `SECRET_KEY`). The raw key is returned exactly once, at creation, with `Cache-Control: no-store`.
  A leaked database therefore does not leak usable keys, and rotating the pepper invalidates every
  key at once — the intended fail-closed response to a leak.
* **Scopes.** `read:tenders`, `read:sources`, `read:documents`, `read:statistics`,
  `read:geography`, `admin`. A route's scope is derived from the first path segment, so
  `/tenders/{id}/documents` needs `read:tenders` (the document is reached *through* the tender) and
  no route can accidentally be left unscoped by forgetting a decorator.
* **Refusals never leak.** Missing, malformed, unknown, expired and revoked keys all answer the same
  401 with `API_KEY_MISSING`/`API_KEY_INVALID`; the prefix of the presented key is never echoed back.
  An insufficient scope is a 403 that names the required scope, because that is guidance, not a
  secret.
* **Minting is an operator action.** `python -m scripts.manage_api_keys create --name ... --scopes ...`
  is the default path, and `POST /api/v1/api-keys` exists only when `API_KEY_SELF_SERVICE_ENABLED=true`
  (an API that hands out its own credentials turns any admin-key leak into unbounded issuance).
  Revocation is immediate and takes effect on the next request, not on token expiry.
* **The metrics scrape is separate.** `/metrics` is excluded from the OpenAPI document and can require
  its own bearer token (`METRICS_TOKEN`), compared in constant time, answering 401 +
  `WWW-Authenticate` so Prometheus retries instead of marking a permanent configuration error.

## Rate limiting

Redis fixed-window enforcement with a per-process fallback, applied **per key** for authenticated
callers and **per client IP** for anonymous ones (20/60/600 requests per minute for
anonymous/authenticated/admin, plus a `RATE_LIMIT_BURST` allowance). Every response carries
`X-RateLimit-Limit`, `-Remaining`, `-Reset` and `-Policy`; refusals are 429 with `Retry-After`.

The `-Policy` header is deliberately part of the contract: when Redis is unreachable the limiter
degrades to in-process enforcement, which is per-replica rather than global, and a client (or a
dashboard) must be able to see that rather than assume the configured limit was applied cluster-wide.

Two failure policies, one knob:

* `RATE_LIMIT_FAIL_OPEN=true` (default) — keep serving with local enforcement. `/health` reports the
  cache as `degraded`, readiness stays green: an optional component's outage must not remove healthy
  nodes from rotation and have an orchestrator restart them mid-incident.
* `RATE_LIMIT_FAIL_OPEN=false` — protected endpoints answer 503
  `RATE_LIMIT_BACKEND_UNAVAILABLE` and readiness fails too, because a node that refuses every data
  request is genuinely not ready. Liveness still returns 200; restarting cannot fix Redis.

Crawl politeness (`HTTP_DEFAULT_RATE_LIMIT_PER_MINUTE`, per-source `rate_limit_per_minute`) is a
separate, outbound limit and is not affected by any of this.

## Not implemented (deliberately)

* **No billing, subscriptions, quotas or per-key quotas** — the brief excludes them; `api_keys` has no
  usage counters or rate columns to unpick later, and the tier→limit policy is a single function
  (`app.services.rate_limit.policy_for`).
* **No OAuth2/JWT, no user accounts, no IP allowlists, no self-service portal.** Keys are issued to
  organisations, not people; if per-IP pinning is ever needed it belongs in `ApiKeyService.verify`.
* **No write API for data.** Source lifecycle changes (verify, activate, pause) are operator actions via
  `scripts/` — deliberately not HTTP endpoints, so a leaked read key cannot reconfigure ingestion. The
  only mutating endpoints in the API are the two admin-scoped key operations.
* **No transport-layer security here.** TLS termination, client certificates and HSTS belong to the
  reverse proxy in front of the API (see `docs/DEPLOYMENT.md`).

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

## Logging, redaction and streams

Structured logs are emitted through one configured pipeline, so three properties hold for *every* line
rather than for the lines emitted after some entrypoint happened to configure logging:

* **Secrets are redacted.** A processor rewrites any key matching
  `api_key|secret|password|token|authorization|passwd` and any `key=value` for those names. Raw API keys
  are never logged at all — only the 12-character prefix — and the digest is not logged either.
* **Correlation is attached.** `request_id`, `source_id` and `job_id` come from context variables read at
  emit time, which is what makes "start the request, then follow one ID through fetch → parse → dedup →
  persist" work without threading arguments through every call.
* **The destination is chosen per write.** Services log to stdout; `scripts/*.py` pass
  `stream=sys.stderr` so stdout stays machine-readable for `--json`. Because the stream is resolved at
  emit time instead of import time, a logger created while the process was still configuring itself
  cannot quietly keep writing somewhere else — which is how a `--json` report once ended up unparsable.

Log level changes take effect for already-created loggers too, since filtering is delegated to the
standard library logger hierarchy rather than baked into a bound logger at creation.

Prometheus scrapes of `/metrics` are anonymous traffic and share that tier's budget; see
[DEPLOYMENT.md](DEPLOYMENT.md#observability) for the interval that stays inside it.
