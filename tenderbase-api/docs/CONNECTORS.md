# Connectors

A **connector** knows how to turn one kind of website or API into `RawItem`s. It never touches the
database, never decides what is a duplicate and never persists anything — the pipeline does that.

Connectors are **data-driven**: there are no per-municipality Python files. A new source is usually
a row in `municipality_sources` pointing at an existing connector with CSS selectors or a field map
in its JSON `config`.

## The contract

```python
class ProcurementConnector(ABC):
    key: str  # registry key, e.g. "html.listing"
    name: str
    connector_type: ConnectorType
    requires_browser: bool = False
    config_schema: dict[str, str]

    async def discover(self, source: SourceContext) -> list[DiscoveryTarget]: ...
    async def fetch(self, source: SourceContext, target: DiscoveryTarget) -> FetchResult: ...
    async def parse(self, source: SourceContext, response: FetchResult) -> list[RawItem]: ...
    def extract_documents(self, source, node, base_url) -> Iterable[DocumentCandidate]: ...
    def validate(self, item: RawItem) -> RawItem: ...
    async def run(self, source: SourceContext) -> AsyncIterator[RawItem]: ...
```

* `SourceContext` is an immutable snapshot of the source row (`base_url`, `config`,
  `municipality_id`, `rate_limit_per_minute`, `robots_policy`, `source_timezone`).
* `RawItem` is stringly-typed on purpose: fields exactly as the publisher wrote them, plus
  `source_url`, `documents[]`, `raw_payload` and `parser_metadata`. Normalization happens later.
* `run()` is the default driver (`discover → fetch → parse`) and is what the pipeline calls; it
  isolates failures per target.

Registration is a decorator; keys must be unique and non-empty:

```python
@register_connector()
class MyConnector(ProcurementConnector):
    key = "html.mything"
```

Importing `app.connectors` registers all built-ins. `GET /api/v1/sources/connectors` exposes the
registry, and `python -m scripts.sync_connectors` mirrors it into `source_connectors`.

## Built-in connectors

| Key | Type | Use it when | Browser |
| --- | --- | --- | --- |
| `http.json` | HTTP | The site exposes a JSON endpoint / open-data API | no |
| `html.listing` | HTML | Classic server-rendered tender table or card list | no |
| `wordpress.rest` | WORDPRESS | The site runs WordPress (`/wp-json/wp/v2`) | no |
| `pdf.repository` | PDF | The "listing" is just a page full of PDF adverts | no |
| `browser.playwright` | BROWSER | The listing is rendered client-side by JavaScript | yes (optional extra) |
| `custom.etender_ocds` | CUSTOM | An OCDS release feed (National Treasury eTender) | no |

### `html.listing`

| Config key | Meaning |
| --- | --- |
| `listing_paths` | Listing page paths (relative to `base_url`) or absolute URLs |
| `item_selector` | CSS selector matching one item per match (**required**) |
| `field_selectors` | `{canonical_field: CSS selector}` |
| `link_selector` | Selector for the item's detail link |
| `document_selector` | Selector for document links inside an item |
| `follow_detail` | Fetch each detail page (default `false`) |
| `detail_field_selectors` | Selectors applied to the detail page |
| `detail_document_selector` | Document selector for detail pages (default: extension-based detection) |
| `pagination` | `{next_selector, max_pages}` — always bounded |

A detail-page failure never loses the listing row; the item is still emitted with
`parser_metadata.detail_error`.

### `http.json`

| Config key | Meaning |
| --- | --- |
| `listing_paths` | Paths or absolute URLs to fetch |
| `records_path` | Dotted path to the record array, e.g. `data.items` |
| `field_map` | `{canonical_field: dotted source path}` |
| `document_path`, `document_url_key` | Where per-record attachments live |
| `detail_url_template` | Optional, e.g. `/tender/{id}` |

A non-JSON body is a `ParseError`, not a silent empty result.

### `wordpress.rest`

| Config key | Meaning |
| --- | --- |
| `post_type` | REST collection (`posts`, `pages`, custom type) |
| `search`, `categories` | Server-side filtering |
| `per_page` (≤100), `max_pages` | Bounded pagination |
| `html_fallback` | An `html.listing` config used when the REST API is disabled (404/non-JSON) |

Document links (PDF, DOC/DOCX, XLS/XLSX) are extracted from the rendered post content and from
`_embedded` media.

### `pdf.repository`

| Config key | Meaning |
| --- | --- |
| `listing_paths` | Pages containing PDF links |
| `link_selector` | Default `a[href$='.pdf']` |
| `max_documents` | Safety ceiling per run (default 100) |
| `extract_pages` | Pages of native text to read per PDF (default 2) |
| `title_from` | `link` (default) or `pdf` |

Native extraction only — OCR is never invoked during discovery.

### `browser.playwright`

Requires the optional `browser` extra and `playwright install chromium`. Config adds
`wait_for_selector` / `wait_ms` to the `html.listing` keys. It renders public pages only: it does
not log in, solve CAPTCHAs or evade bot protection. If a site blocks automated access, the correct
response is to stop and mark the source, not to work around the control.

### `custom.etender_ocds`

Parses OCDS 1.1 release packages (National Treasury publishes procurement data as OCDS).

| Config key | Meaning |
| --- | --- |
| `listing_paths` | OCDS release endpoints — **required**, taken from the portal's own API documentation |
| `releases_path` | Dotted path to the releases array (default `releases`) |
| `next_link_path` | Dotted path to the next-page link (default `links.next`) |
| `max_pages` | Pagination safety limit (default 5) |

It maps `tender.title`, `tender.procurementMethodDetails`, `tender.tenderPeriod.endDate`,
`tender.value`, `buyer.name`, `tender.documents[]`, `parties[].contactPoint` and the award/contract
blocks, and preserves the whole release in `raw_payload`.

> **No endpoint is hard-coded.** `discover()` raises `ParseError` when `listing_paths` is missing,
> precisely so nobody ships a guessed URL. The parser is exercised against
> `tests/fixtures/etender_ocds_release.json`; it has **not** been verified against live traffic
> from this build environment. Before enabling it, read the portal's current API documentation and
> put the real endpoint in the source config.

## Writing a new connector

1. Ask first whether an existing connector plus configuration can do the job. It usually can.
2. Subclass `ProcurementConnector`, set `key`, `name`, `connector_type`, `config_schema`, and
   decorate with `@register_connector()`.
3. Implement `discover`, `fetch` and `parse`. Use `self.fetcher` for all HTTP so you inherit SSRF
   validation, redirect limits, size caps, retries, rate limiting and robots handling.
4. Emit values **exactly as published** — no cleaning, no guessing. Missing field → omit it.
5. Save a real (anonymised, `example.org`) response as a fixture in `tests/fixtures/` and write a
   test using the `mock_fetcher` fixture. Connector tests never touch the network.
6. Run `python -m scripts.sync_connectors` so the registry mirror and the API catalogue include it.

## Failure semantics

| Situation | Behaviour |
| --- | --- |
| One listing page 404s | `StageError(FETCH)` recorded; other pages continue |
| One item unparsable | `StageError(PARSE)`; the rest of the page still yields items |
| Whole source unreachable | Run marked `FAILED`, health downgraded, next run backed off — the source is never auto-disabled |
| robots.txt disallows the URL | `RobotsDisallowedError`; the URL is skipped and recorded |
