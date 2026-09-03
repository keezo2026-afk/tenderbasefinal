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
4. **Storage** — through the `BlobStorage` ABC. `LocalBlobStorage` ships; an S3-compatible backend
   is the documented extension point. Path traversal is rejected on both read and write.
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
* Storage is configured with `STORAGE_BACKEND` (`local`) and `STORAGE_LOCAL_PATH`; in Docker the
  path is a named volume shared by the API and worker containers.
* The API serves document metadata and extracted text only — it is not a file proxy. Serving the
  binaries is a deployment decision (signed URLs from object storage, or not at all).
* Nothing is deleted automatically. Blob retention is an operator policy, not a hidden behaviour.
