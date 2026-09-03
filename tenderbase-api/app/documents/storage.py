"""Blob storage abstraction.

Large binary/raw payloads never belong in PostgreSQL. Everything the platform
stores on disk goes through this interface, so switching to S3/MinIO later is a
configuration change rather than a rewrite.

Storage keys are always derived from content hashes — never from user- or
site-supplied filenames — which also eliminates path-traversal risk.
"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.config import Settings, get_settings
from app.errors import DocumentError
from app.utils.hashing import sha256_text


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Metadata describing a stored blob."""

    key: str
    size: int
    backend: str


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
    def open_write(self, key: str) -> BinaryIO:
        """Open a binary writable stream for ``key`` (large downloads)."""

    @abstractmethod
    def read_bytes(self, key: str) -> bytes: ...

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

    def open_write(self, key: str) -> BinaryIO:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("wb")

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


_S3_UNAVAILABLE = "The S3 storage backend is not implemented yet."


class S3BlobStorage(BlobStorage):  # pragma: no cover - not implemented yet
    """Planned S3/MinIO backend.

    Deliberately unimplemented: the interface exists so the rest of the system
    is storage-agnostic. Raising here is honest — it is never silently
    substituted with local storage in production.
    """

    backend = "s3"

    def __init__(self, *_: object, **__: object) -> None:
        raise NotImplementedError(
            "The S3 storage backend is not implemented yet. Set "
            "DOCUMENT_STORAGE_BACKEND=local, or implement S3BlobStorage."
        )

    # Bodies, not signature stubs: a stub returns None, which satisfies a `bool` or
    # `int` annotation at a glance and silently corrupts whatever reads it. Only a
    # raised error makes an unimplemented backend impossible to mistake for a working
    # one. (`__init__` already refuses, so these guard a future subclass that forgets
    # a method.)
    def write_bytes(self, key: str, data: bytes) -> StoredObject:
        raise NotImplementedError(_S3_UNAVAILABLE)

    def open_write(self, key: str) -> BinaryIO:
        raise NotImplementedError(_S3_UNAVAILABLE)

    def read_bytes(self, key: str) -> bytes:
        raise NotImplementedError(_S3_UNAVAILABLE)

    def exists(self, key: str) -> bool:
        raise NotImplementedError(_S3_UNAVAILABLE)

    def delete(self, key: str) -> None:
        raise NotImplementedError(_S3_UNAVAILABLE)

    def size(self, key: str) -> int:
        raise NotImplementedError(_S3_UNAVAILABLE)


def get_document_storage(settings: Settings | None = None) -> BlobStorage:
    """Factory for the configured document store."""
    cfg = settings or get_settings()
    if cfg.document_storage_backend == "s3":
        return S3BlobStorage()
    return LocalBlobStorage(cfg.document_storage_path)


def get_raw_storage(settings: Settings | None = None) -> BlobStorage:
    """Factory for the raw-payload (audit) store."""
    cfg = settings or get_settings()
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
