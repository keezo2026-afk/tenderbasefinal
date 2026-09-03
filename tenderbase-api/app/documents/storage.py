"""Blob storage abstraction.

Large binary/raw payloads never belong in PostgreSQL. Everything the platform
stores on disk goes through this interface, so switching to S3/MinIO later is a
configuration change rather than a rewrite.

Storage keys are always derived from content hashes — never from user- or
site-supplied filenames — which also eliminates path-traversal risk.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, cast

from app.config import Settings, get_settings
from app.errors import DocumentError
from app.utils.hashing import sha256_text


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Metadata describing a stored blob."""

    key: str
    size: int
    backend: str


#: The two stream shapes this interface deals in, declared as protocols rather than
#: ``BinaryIO``: an S3 upload has no file descriptor behind it, and pretending it does
#: (subclassing ``IOBase`` for show) is how a caller ends up calling ``flush()`` on a
#: buffer that is not flushed to anything. A local file object satisfies both protocols.
class WriteStream(Protocol):
    """Something bytes are written to, uploaded or persisted when closed."""

    def write(self, data: bytes) -> int: ...

    def close(self) -> None: ...

    def __enter__(self) -> WriteStream: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None: ...


class ReadStream(Protocol):
    """Something bytes are read from, with no requirement that the whole object fits in RAM."""

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...

    def __enter__(self) -> ReadStream: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None: ...


#: 16 MiB in memory before an S3 write spills to disk — the same order of size as the
#: per-document limit, so a normal document never touches the temp directory.
S3_SPOOL_BYTES = 16 * 1024 * 1024


def build_storage_key(*, namespace: str, digest: str, extension: str | None = None) -> str:
    """Content-addressed key: ``namespace/ab/cd/<sha256>.<ext>``.

    Only hex digest characters and a short sanitised extension are used, so the
    resulting path can never escape the storage root.
    """
    clean_digest = "".join(ch for ch in digest.lower() if ch in "0123456789abcdef")
    if len(clean_digest) < 8:
        raise DocumentError("A storage key requires a valid hex digest")
    safe_namespace = "".join(ch for ch in namespace if ch.isalnum() or ch in "-_") or "misc"
    suffix = ""
    if extension:
        ext = "".join(ch for ch in extension.lower() if ch.isalnum())[:8]
        if ext:
            suffix = f".{ext}"
    return f"{safe_namespace}/{clean_digest[:2]}/{clean_digest[2:4]}/{clean_digest}{suffix}"


class BlobStorage(ABC):
    """Minimal blob-store interface."""

    backend: str = "abstract"

    @abstractmethod
    def write_bytes(self, key: str, data: bytes) -> StoredObject: ...

    @abstractmethod
    def open_write(self, key: str) -> WriteStream:
        """Open a writable stream for ``key`` (large downloads, never buffered whole)."""

    @abstractmethod
    def read_bytes(self, key: str) -> bytes: ...

    @abstractmethod
    def open_read(self, key: str) -> ReadStream:
        """Open a readable stream for ``key``.

        Part of the interface rather than an S3 extra: serving a stored document should
        not have to hold the whole file in memory, and a backend that could stream but
        had no way to say so would quietly be the reason a large PDF OOM'd a worker.
        """

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def size(self, key: str) -> int: ...


class LocalBlobStorage(BlobStorage):
    """Filesystem-backed storage rooted at a configured directory."""

    backend = "local"

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        if not str(candidate).startswith(str(self.root) + os.sep):
            raise DocumentError("Refusing to access a path outside the storage root")
        return candidate

    def write_bytes(self, key: str, data: bytes) -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_bytes(data)
        temporary.replace(path)
        return StoredObject(key=key, size=len(data), backend=self.backend)

    def open_write(self, key: str) -> WriteStream:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("wb")  # type: ignore[return-value]

    def open_read(self, key: str) -> ReadStream:
        path = self._path(key)
        if not path.exists():
            raise DocumentError(f"Object not found: {key}")
        return path.open("rb")  # type: ignore[return-value]

    def read_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise DocumentError(f"Object not found: {key}")
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def size(self, key: str) -> int:
        path = self._path(key)
        return path.stat().st_size if path.exists() else 0

    def clear(self) -> None:  # pragma: no cover - maintenance helper
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)


class _S3WriteStream:
    """A writable file-like object that uploads to S3 when closed.

    ``BlobStorage.open_write`` is how the downloader stores a large file without ever
    holding it in memory, so the S3 backend cannot simply buffer everything: the payload
    is spooled to a temporary file and streamed with ``upload_fileobj`` (which switches to
    multipart on its own above the chunk threshold). Aborting before close uploads nothing,
    which is exactly what a failed download needs.
    """

    def __init__(self, storage: S3BlobStorage, key: str) -> None:
        self._storage = storage
        self._key = key
        self._buffer = tempfile.SpooledTemporaryFile(max_size=S3_SPOOL_BYTES)
        self._closed = False

    def write(self, data: bytes) -> int:
        return self._buffer.write(data)

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._buffer.seek(0)
        try:
            # ``SpooledTemporaryFile`` is a real file object once it rolls over, and
            # boto3 accepts anything with ``read``/``seek``/``tell``; the cast is for
            # typeshed, which models the spool as its own unrelated type.
            self._storage.upload_fileobj(cast("BinaryIO", self._buffer), self._key)
        finally:
            self._buffer.close()

    def abort(self) -> None:
        """Discard the payload: nothing is uploaded, and no partial object appears."""
        self._closed = True
        self._buffer.close()

    def __enter__(self) -> _S3WriteStream:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()


#: Object keys this backend will accept. ``build_storage_key`` produces keys from a hex
#: digest under a sanitised namespace, and the S3 backend re-checks that shape instead of
#: trusting the caller: on a filesystem a bad key escapes a *root*, on S3 a bad key
#: escapes the *bucket prefix*, which is where another tenant's objects live.
_KEY_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def validate_object_key(key: str) -> str:
    """Return ``key`` if it is a safe, normalised object key, else raise."""
    if not key or len(key) > 1024:
        raise DocumentError("Object key must be a non-empty string of at most 1024 chars")
    if key.startswith("/") or key.endswith("/") or "//" in key or "\\" in key:
        raise DocumentError(f"Object key is not in canonical form: {key!r}")
    for segment in key.split("/"):
        if segment in (".", "..") or not _KEY_SEGMENT.match(segment):
            raise DocumentError(f"Object key contains an unsafe segment: {key!r}")
    return key


class S3BlobStorage(BlobStorage):
    """S3/MinIO blob store: content-addressed keys, private objects, streaming I/O.

    Nothing here assumes the bucket is public, and nothing makes it public: objects are
    written with no canned ACL (the bucket's own ownership/block-public-access settings
    govern), reads go through the bucket policy that the API's credentials satisfy, and a
    direct link for a client is available only as a short presigned URL behind an explicit
    opt-in. Keys are always the caller's validated, content-derived key under a fixed
    prefix — a filename from a source website can never reach an object key.
    """

    backend = "s3"

    def __init__(self, settings: Settings | None = None) -> None:
        cfg = settings or get_settings()
        try:
            import boto3  # noqa: F401  (optional dependency: [s3] extra)
            from botocore.config import Config as BotoConfig
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise DocumentError(
                "DOCUMENT_STORAGE_BACKEND=s3 needs boto3: install the service with "
                "``pip install tenderbase-api[s3]`` (or ``pip install boto3``), or set "
                "DOCUMENT_STORAGE_BACKEND=local"
            ) from exc

        if not cfg.s3_bucket.strip():
            raise DocumentError("DOCUMENT_STORAGE_BACKEND=s3 requires S3_DOCUMENT_BUCKET to be set")

        self.bucket = cfg.s3_bucket.strip()
        self.prefix = cfg.s3_key_prefix.strip().strip("/")
        self.encryption = cfg.s3_server_side_encryption.strip()
        self.presigned_enabled = cfg.s3_presigned_urls_enabled
        self.presigned_seconds = cfg.s3_presigned_url_seconds

        kwargs: dict[str, object] = {
            "region_name": cfg.s3_region or None,
            "config": BotoConfig(
                signature_version="s3v4",
                # Retries are configured rather than defaulting: a transient 503 from an
                # endpoint is routine, and an unbounded retry loop inside a request is how
                # a blob store outage becomes a pile of timed-out API calls.
                retries={"max_attempts": 5, "mode": "adaptive"},
                connect_timeout=cfg.s3_connect_timeout_seconds,
                read_timeout=cfg.s3_read_timeout_seconds,
                s3={"addressing_style": "path" if cfg.s3_force_path_style else "virtual"},
            ),
        }
        if cfg.s3_endpoint_url.strip():
            kwargs["endpoint_url"] = cfg.s3_endpoint_url.strip()
        if cfg.s3_access_key_id and cfg.s3_secret_access_key:
            # Only both-or-none reaches here (the settings validator refuses a half pair),
            # so this never silently falls back to an ambient identity.
            kwargs["aws_access_key_id"] = cfg.s3_access_key_id
            kwargs["aws_secret_access_key"] = cfg.s3_secret_access_key
        self._client = boto3.client("s3", **kwargs)  # type: ignore[arg-type]

    # -- keys --------------------------------------------------------------

    def object_key(self, key: str) -> str:
        """The full bucket key for a storage key (validated, prefixed)."""
        safe = validate_object_key(key)
        return f"{self.prefix}/{safe}" if self.prefix else safe

    # -- blob API -----------------------------------------------------------

    def write_bytes(self, key: str, data: bytes) -> StoredObject:
        full = self.object_key(key)
        request: dict[str, object] = {"Bucket": self.bucket, "Key": full, "Body": data}
        if self.encryption:
            request["ServerSideEncryption"] = self.encryption
        self._client.put_object(**request)  # type: ignore[arg-type]
        return StoredObject(key=key, size=len(data), backend=self.backend)

    def upload_fileobj(self, fileobj: BinaryIO, key: str) -> StoredObject:
        """Stream an open binary file into ``key`` (multipart above the chunk size)."""
        full = self.object_key(key)
        self._client.upload_fileobj(fileobj, self.bucket, full, ExtraArgs=self._extra_args())
        return StoredObject(key=key, size=-1, backend=self.backend)

    def read_bytes(self, key: str) -> bytes:
        return self._get(key).read()

    def open_read(self, key: str) -> ReadStream:
        """A streaming reader for an object, for serving a file without buffering it."""
        return self._get(key)

    def open_write(self, key: str) -> WriteStream:
        validate_object_key(key)
        return _S3WriteStream(self, key)

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self.object_key(key))
        except _client_errors() as exc:
            if _is_missing_object(exc):
                return False
            if _is_missing_bucket(exc):
                raise DocumentError(
                    f"S3 bucket {self.bucket!r} does not exist; check S3_BUCKET, "
                    "S3_REGION and S3_ENDPOINT_URL"
                ) from exc
            # Anything else (denied credentials, wrong region, a 5xx) is re-raised as an
            # error rather than answered with "does not exist": the downloader would
            # re-store an object that is actually there, and an operator reading the
            # health endpoint would be told the evidence is gone.
            raise DocumentError(f"Object metadata unavailable for {key}: {exc}") from exc
        return True

    def size(self, key: str) -> int:
        try:
            head = self._client.head_object(Bucket=self.bucket, Key=self.object_key(key))
        except _client_errors() as exc:
            if _is_missing_object(exc):
                return 0
            raise DocumentError(f"Object metadata unavailable for {key}: {exc}") from exc
        return int(head.get("ContentLength", 0))

    def delete(self, key: str) -> None:
        # Delete is idempotent in S3, which matches the local backend's behaviour and is
        # what a retry after a partial cleanup needs.
        self._client.delete_object(Bucket=self.bucket, Key=self.object_key(key))

    def presigned_get_url(self, key: str, *, expires_seconds: int | None = None) -> str:
        """A short-lived GET URL for a client, when the deployment opted in.

        Refused unless ``S3_PRESIGNED_URLS_ENABLED=true``: a signed URL is a bearer
        credential that survives the API's authentication, so it is a deliberate
        exchange rather than a convenience.
        """
        if not self.presigned_enabled:
            raise DocumentError(
                "Presigned document URLs are disabled; set S3_PRESIGNED_URLS_ENABLED=true "
                "only when the bucket keeps objects private and a direct client fetch is "
                "required"
            )
        return str(
            self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": self.object_key(key)},
                ExpiresIn=int(expires_seconds or self.presigned_seconds),
            )
        )

    # -- internals ----------------------------------------------------------

    def _extra_args(self) -> dict[str, str]:
        return {"ServerSideEncryption": self.encryption} if self.encryption else {}

    def _get(self, key: str) -> _StreamingBodyReader:
        try:
            body = self._client.get_object(Bucket=self.bucket, Key=self.object_key(key))["Body"]
        except _client_errors() as exc:
            if _is_missing_object(exc):
                raise DocumentError(f"Object not found: {key}") from exc
            if _is_missing_bucket(exc):
                raise DocumentError(
                    f"S3 bucket {self.bucket!r} does not exist; check S3_BUCKET, "
                    "S3_REGION and S3_ENDPOINT_URL"
                ) from exc
            raise DocumentError(f"Object read failed for {key}: {exc}") from exc
        return _StreamingBodyReader(body)

    def close(self) -> None:  # pragma: no cover - the client has no required shutdown
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


def _client_errors() -> tuple[type[BaseException], ...]:
    """The exception type botoc3 raises for a service error, imported lazily.

    boto3 is an optional dependency: importing it at module load would make
    ``app.documents.storage`` unimportable in a deployment that only ever uses local
    storage, which is the common case and must keep working.
    """
    from botocore.exceptions import ClientError

    return (ClientError,)


def _is_missing_object(exc: object) -> bool:
    """Whether a ``ClientError`` means "no such object", and nothing else.

    ``NoSuchBucket`` counts: an object in a deleted bucket is absent for every purpose
    this store has (re-store it, report it missing). A credential error, a wrong region
    or a throttle deliberately does **not**, because reading those as "absent" turns an
    outage into silent data loss.
    """
    code = ""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code", ""))
    return code in {"404", "NoSuchKey", "NotFound"}


def _is_missing_bucket(exc: object) -> bool:
    """Whether the *bucket* is what S3 says is missing.

    Kept separate from a missing key on purpose: "this object is not here" is a fact the
    caller can act on (store it, report it), while "this bucket does not exist" is a
    misconfiguration that must not be dressed up as either.
    """
    code = ""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code", ""))
    return code == "NoSuchBucket"


class _StreamingBodyReader:
    """Adapts botocore's streaming body to the ``BinaryIO`` the interface promises."""

    def __init__(self, body: object) -> None:
        self._body = body

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            return bytes(self._body.read())  # type: ignore[attr-defined]
        return bytes(self._body.read(size))  # type: ignore[attr-defined]

    def readable(self) -> bool:
        return True

    def __iter__(self):  # noqa: ANN204 - line-wise chunk iteration for callers
        return iter(self._body)  # type: ignore[attr-defined]

    def close(self) -> None:
        close = getattr(self._body, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> _StreamingBodyReader:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def get_document_storage(settings: Settings | None = None) -> BlobStorage:
    """Factory for the configured document store."""
    cfg = settings or get_settings()
    if cfg.document_storage_backend == "s3":
        return S3BlobStorage(cfg)
    return LocalBlobStorage(cfg.document_storage_path)


def get_raw_storage(settings: Settings | None = None) -> BlobStorage:
    """Factory for the raw-payload (audit) store.

    Same backend as documents, deliberately: the raw payload is the evidence behind a
    record, and keeping evidence on a disk that no backup job reads is how audits end up
    with nothing to check. The two namespaces stay separate keys in the bucket.
    """
    cfg = settings or get_settings()
    if cfg.document_storage_backend == "s3":
        return S3BlobStorage(cfg)
    return LocalBlobStorage(cfg.raw_payload_storage_path)


def store_raw_payload(
    payload: str, *, namespace: str = "raw", settings: Settings | None = None
) -> str:
    """Persist a raw payload (HTML/JSON) and return its storage key."""
    storage = get_raw_storage(settings)
    digest = sha256_text(payload)
    key = build_storage_key(namespace=namespace, digest=digest, extension="txt")
    if not storage.exists(key):
        storage.write_bytes(key, payload.encode("utf-8"))
    return key


__all__ = [
    "BlobStorage",
    "LocalBlobStorage",
    "ReadStream",
    "S3BlobStorage",
    "StoredObject",
    "WriteStream",
    "build_storage_key",
    "get_document_storage",
    "get_raw_storage",
    "store_raw_payload",
    "validate_object_key",
]
