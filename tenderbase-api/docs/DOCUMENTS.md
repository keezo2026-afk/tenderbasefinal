# Documents

Municipal procurement lives in attachments: bid documents, SBD forms, specifications, addenda,
briefing minutes and award notices — overwhelmingly PDF. TenderBase treats documents as
first-class, content-addressed objects.

## Identity: SHA-256, not filenames

Every municipality publishes `document.pdf`, `Advert.pdf` and `SBD4.pdf`. Filenames are worthless
as identity, so:

* the SHA-256 of the bytes is the document's identity;
* the same bytes discovered on two opportunities are stored once in blob storage;
* a *re-uploaded* file with different bytes becomes a **new version** of the same document row
  (`UNIQUE (document_id, sha256)`), not a duplicate document;
* if the hash is unchanged, nothing is re-stored and no version is created.

Storage keys are sharded to keep directories small:

```
documents/<hash[0:2]>/<hash[2:4]>/<hash>.pdf
```

## Lifecycle

```
link discovered ─▶ download ─▶ sniff format ─▶ sha256 ─▶ store ─▶ version ─▶ extract text ─▶ (OCR) ─▶ classify
```

1. **Discovery** — connectors record links only (`is_downloaded=false`, `sha256=null`). Ingestion
   never blocks on a 30 MB PDF.
2. **Download** — `documents/downloader.py` uses the same guarded fetcher as ingestion:
   SSRF-validated URL, scheme allowlist, redirect cap, timeout, and a hard byte ceiling
   (`HTTP_MAX_RESPONSE_BYTES`, default 25 MiB). `ETag`/`Last-Modified` are honoured for
   conditional re-downloads.
3. **Format sniffing** — magic bytes first, declared content type second, filename last. A
   `.pdf` that is really HTML is recorded as HTML.
4. **Storage** — through the `BlobStorage` ABC, implemented by `LocalBlobStorage` and
   `S3BlobStorage` (`DOCUMENT_STORAGE_BACKEND=s3`). A path-traversal-shaped key is rejected on read
   and on write, on both backends.
5. **Versioning** — a new hash creates a `document_versions` row and bumps `current_version`.
6. **Text extraction** — see below.
7. **Classification** — rule-based typing from filename, link text and extracted text.

Failures never raise into the caller: `download_error` is recorded on the document and the batch
continues.

## Text extraction

| Format | Method | Library |
| --- | --- | --- |
| PDF (digital) | `NATIVE_PDF` | pypdf, with pdfplumber for layout-sensitive pages |
| PDF (scanned) | `OCR` | optional, see below |
| HTML | `HTML_PARSE` | BeautifulSoup with scripts/styles stripped |
| TXT | `PLAIN_TEXT` | stdlib |
| CSV | `SPREADSHEET` | stdlib csv |
| DOCX | `PLAIN_TEXT` | OOXML parsed directly (no extra dependency) |
| Legacy DOC, XLS/XLSX | not implemented | returns empty text with method `NONE` — never a fabricated body |

Extraction results carry provenance: `extraction_method`, `char_count`, `page_count`,
`extraction_confidence`, `ocr_used` and a `structure` blob (per-page char counts). The API exposes
all of it at `/documents/{id}/text`; when nothing has been extracted the endpoint returns **404**
rather than an empty-but-plausible body.

## OCR policy

**Native text first, always.** OCR runs only when *all* of these hold:

* `OCR_ENABLED=true` (default **false**);
* the document is a PDF;
* native extraction yielded less than ~40 characters per page (i.e. it is a scan).

OCR is expensive, error-prone and hallucination-adjacent for tables, so its output is marked
`ocr_used=true` with a lower `extraction_confidence`; downstream consumers can treat it
accordingly. `app/documents/ocr.py` defines the interface and the trigger heuristic; the concrete
engine (`pytesseract` + `pdf2image`, both optional extras plus system packages) is wired in at
deployment time. Without those packages the module reports OCR as unavailable — it does not pretend
to have read the page.

## Classification

`app/documents/classifier.py` maps documents to `DocumentType` using ordered rules over filename,
link text and (when available) the first page of text:

| Signal | Type |
| --- | --- |
| `addendum`, `amendment`, `corrigendum` | `ADDENDUM` |
| `SBD`, `MBD`, `declaration`, `returnable` | `BID_FORM` |
| `specification`, `scope of work`, `TOR` | `SPECIFICATION` |
| `briefing`, `site meeting`, `minutes` | `BRIEFING_NOTES` |
| `award`, `appointment`, `successful bidder` | `AWARD_NOTICE` |
| `RFQ`, `request for quotation` | `RFQ_DOCUMENT` |
| `tender`, `bid document` | `TENDER_DOCUMENT` |
| no confident signal | `UNKNOWN` |

Each classification carries a confidence; `UNKNOWN` is a perfectly acceptable answer and is left
alone rather than being forced into a bucket. AI-assisted classification is an optional future
enhancement behind the `AIProvider` interface, disabled by default.

## Operational notes

* Pending downloads are processed by the worker (`process_pending_documents`), in bounded batches.
* Storage is configured with `DOCUMENT_STORAGE_BACKEND` (`local` | `s3`) plus
  `DOCUMENT_STORAGE_PATH`, `RAW_PAYLOAD_STORAGE_PATH` or the `S3_*` settings described below. In
  Docker the local path is a named volume shared by the API and worker containers.
* Raw ingestion payloads (`raw_payload_key`) follow the same switch, so evidence never stays on a
  disk that a backup job does not read.

## Blob storage backends

| Setting | Default | Meaning |
| --- | --- | --- |
| `DOCUMENT_STORAGE_BACKEND` | `local` | `local` or `s3`; also governs raw-payload offload |
| `DOCUMENT_STORAGE_PATH` | `./data/documents` | Local blob root (`local` backend only) |
| `RAW_PAYLOAD_STORAGE_PATH` | `./data/raw` | Local raw-payload root (`local` backend only) |
| `S3_BUCKET` | — | Required when the backend is `s3`; startup fails without it |
| `S3_REGION` | `af-south-1` | Signing region; also how AWS endpoints are resolved when `S3_ENDPOINT_URL` is empty |
| `S3_ENDPOINT_URL` | — | S3-compatible endpoint (MinIO, R2, Ceph); empty means AWS |
| `S3_FORCE_PATH_STYLE` | `false` | `endpoint/bucket/key` addressing, which most MinIO setups need; AWS wants the default virtual-host style |
| `S3_KEY_PREFIX` | `tenderbase` | Namespace joined in front of every key. Each segment is limited to `[A-Za-z0-9._-]` and dot segments are refused, because a loose prefix would be an escape hatch |
| `S3_SERVER_SIDE_ENCRYPTION` | `AES256` | Per-object SSE header; set empty only when the bucket enforces encryption another way |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | — | Both or neither (a half pair is a startup error). Left empty, the default AWS provider chain is used — instance role, ECS task identity, shared profile |
| `S3_CONNECT_TIMEOUT_SECONDS` / `S3_READ_TIMEOUT_SECONDS` | `5.0` / `60.0` | boto3 timeouts; the read timeout applies per chunk while streaming |
| `S3_PRESIGNED_URLS_ENABLED` | `false` | Must be true before a presigned GET will be minted |
| `S3_PRESIGNED_URL_SECONDS` | `300` | Lifetime of such a URL (1 s – 7 d) |

Retries are fixed in the client configuration (`adaptive`, at most 5 attempts including the
first) rather than a setting: a document download already has its own retry policy one level up,
and two independent backoffs on the same failure make an outage look longer than it is.

Operator-visible rules:

* **Keys are content-addressed and internal.** `sha256(content)` decides the object key
  (`documents/ab/cd/<hash>.pdf` for files, `html/ab/cd/<hash>` for raw payloads); a filename or URL
  from a source can never select a path, and `S3_KEY_PREFIX` is joined by the backend, not by
  callers.
* **Nothing is made public by the application.** No canned ACL is sent on write (asserted in tests),
  so objects stay private whatever the bucket otherwise allows — a public bucket is never assumed and
  never required. `S3BlobStorage.presigned_get_url` exists for deployments that decide to hand out a
  temporary direct read, and it refuses to sign anything while `S3_PRESIGNED_URLS_ENABLED=false`.
  **No API endpoint currently calls it**: the HTTP API keeps serving metadata and extracted text only,
  so a direct link would have to be minted by an operator tool first.
* **Switching backends does not move data.** Existing objects keep living where they were written;
  the factory only decides where *new* writes and reads look. A migration means copying the prefix
  (for example `aws s3 cp --recursive`) or re-running ingestion.
* **A missing bucket is an error, not an absence.** `exists()` is false only for `404`/`NoSuchKey`;
  credential failures, `NoSuchBucket` and 5xx propagate as `DocumentError` so an outage cannot be
  recorded as "the evidence was deleted".
* **Writes are streamed.** The API never materialises a whole object in memory: the download writes
  through a spooled handle (16 MiB in RAM, then temp-dir backed) and uploads with `upload_fileobj`,
  and the stream is aborted if the download fails, so a partial body never appears in the bucket.
* **The SDK is an optional extra.** `boto3` is imported lazily, so a local-only deployment installs
  nothing extra and an `s3` deployment that forgot the dependency fails with a message naming
  `pip install "tenderbase-api[s3]"` rather than an `ImportError` at import time of the whole app.
  Container images that want the backend must add the extra at build time; this repository's image
  does not include it.

* Verified against `moto` (in-process S3 mock) with 41 tests covering round-trips, streaming,
  key safety, prefix handling, encryption headers and factory routing — **not** against a live AWS
  bucket, which needs credentials this environment does not have.
* The API serves document metadata and extracted text only — it is not a file proxy. Serving the
  binaries is a deployment decision (signed URLs from object storage, or not at all).
* Nothing is deleted automatically. Blob retention is an operator policy, not a hidden behaviour.
