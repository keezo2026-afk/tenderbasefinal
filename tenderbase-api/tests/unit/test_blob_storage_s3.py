"""The S3 blob backend, tested against moto — no AWS account, no credentials.

Sprint 1.5 objective 7. ``S3BlobStorage`` existed as a class that raised on
construction, which was honest but left the abstraction unproven: nobody knew whether
``open_write`` could be satisfied by an object store at all (there is no file to write
to), whether a source-supplied filename could become an object key, or what a missing
object should look like to a caller.

``moto`` implements the S3 API in-process, so these are real signed requests against a
real XML layer with real ``NoSuchKey`` errors — not a stubbed client — while the suite
stays hermetic and offline. Anything that would need a *bucket policy* to evaluate
("is the object public?") is instead asserted structurally: no ACL is ever sent, and a
presigned URL cannot be minted unless the deployment opts into it.
"""

from __future__ import annotations

import io
import sys
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.documents.storage import (
    LocalBlobStorage,
    S3BlobStorage,
    build_storage_key,
    get_document_storage,
    get_raw_storage,
    store_raw_payload,
    validate_object_key,
)
from app.errors import DocumentError
from app.utils.hashing import sha256_bytes, sha256_text

pytestmark = pytest.mark.unit

boto3 = pytest.importorskip("boto3", reason="the s3 extra (boto3) is not installed")
moto = pytest.importorskip("moto", reason="moto is not installed; S3 tests need it")

BUCKET = "tenderbase-test-fixtures"


def s3_settings(**overrides: Any) -> Settings:
    """Settings for an S3-backed deployment. Development data, never a real bucket."""
    base: dict[str, Any] = {
        "app_env": "test",
        "document_storage_backend": "s3",
        "s3_bucket": BUCKET,
        "s3_region": "af-south-1",
        "s3_key_prefix": "tenderbase",
        "s3_server_side_encryption": "AES256",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def s3_client() -> Any:
    """A mocked S3 with one private bucket, and nothing else."""
    with moto.mock_aws():
        client = boto3.client("s3", region_name="af-south-1")
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "af-south-1"}
        )
        yield client


@pytest.fixture
def storage(s3_client: Any) -> S3BlobStorage:
    return S3BlobStorage(s3_settings())


def key_for(data: bytes, *, extension: str = "pdf") -> str:
    return build_storage_key(namespace="documents", digest=sha256_bytes(data), extension=extension)


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------


def test_roundtrip_metadata_and_deletion(storage: S3BlobStorage, s3_client: Any) -> None:
    payload = b"%PDF-1.4 TEST FIXTURE document\n" + b"x" * 5000
    key = key_for(payload)

    stored = storage.write_bytes(key, payload)
    assert stored.backend == "s3"
    assert stored.size == len(payload)
    assert stored.key == key

    assert storage.exists(key) is True
    assert storage.size(key) == len(payload)
    assert storage.read_bytes(key) == payload

    storage.delete(key)
    assert storage.exists(key) is False
    assert storage.size(key) == 0
    with pytest.raises(DocumentError, match="Object not found"):
        storage.read_bytes(key)
    # Deleting what is already gone is a no-op, so a retried cleanup cannot fail a job.
    storage.delete(key)


def test_identical_content_is_stored_once(storage: S3BlobStorage, s3_client: Any) -> None:
    """Content addressing: the key is the hash, so a repeat download is an idempotent write."""
    payload = b"same bytes twice"
    first = storage.write_bytes(key_for(payload), payload)
    second = storage.write_bytes(key_for(payload), payload)
    assert first.key == second.key
    listed = s3_client.list_objects_v2(Bucket=BUCKET)
    assert listed["KeyCount"] == 1


def test_streaming_write_uploads_on_close(storage: S3BlobStorage) -> None:
    """``open_write`` is how the downloader streams a large file; nothing is buffered whole."""
    key = key_for(b"streamed")
    with storage.open_write(key) as stream:
        for chunk in (b"chunk-one:", b"chunk-two:", b"chunk-three"):
            stream.write(chunk)
    assert storage.read_bytes(key) == b"chunk-one:chunk-two:chunk-three"
    assert storage.size(key) == len(b"chunk-one:chunk-two:chunk-three")


def test_abandoned_stream_stores_nothing(storage: S3BlobStorage, s3_client: Any) -> None:
    """A download that fails mid-body must not leave a half-written object behind."""
    key = key_for(b"abandoned")
    with pytest.raises(RuntimeError, match="connection reset"):
        with storage.open_write(key) as stream:
            stream.write(b"partial bytes")
            raise RuntimeError("connection reset")
    assert storage.exists(key) is False


def test_streaming_read_returns_every_byte(storage: S3BlobStorage) -> None:
    payload = bytes(range(256)) * 400
    key = key_for(payload)
    storage.write_bytes(key, payload)

    chunks: list[bytes] = []
    with storage.open_read(key) as reader:
        while True:
            part = reader.read(4096)
            if not part:
                break
            chunks.append(part)
    assert b"".join(chunks) == payload


def test_a_file_object_can_be_uploaded_directly(storage: S3BlobStorage) -> None:
    key = key_for(b"from a buffer")
    storage.upload_fileobj(io.BytesIO(b"from a buffer"), key)
    assert storage.read_bytes(key) == b"from a buffer"


# ---------------------------------------------------------------------------
# Key safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "/absolute/key.pdf",
        "leading//double.pdf",
        "trailing/",
        "a/b/../c.pdf",
        "../escape.pdf",
        "..",
        ".",
        "back\\slash.pdf",
        "with space.pdf",
        "quote'in.pdf",
        "\x00nul.pdf",
        "x" * 129 + ".pdf",
    ],
)
def test_unsafe_object_keys_are_refused_before_any_request(
    storage: S3BlobStorage, s3_client: Any, bad_key: str
) -> None:
    """Traversal-shaped keys are rejected locally, not sent to the bucket.

    On a filesystem a bad path escapes a root; in a bucket it escapes the *prefix*, and
    the prefix is what keeps several deployments sharing one bucket apart. The assertion
    that matters is the second one: nothing was written anywhere, so a caller cannot
    probe the backend with a crafted key.
    """
    with pytest.raises(DocumentError):
        storage.write_bytes(bad_key, b"payload")
    with pytest.raises(DocumentError):
        validate_object_key(bad_key)
    assert s3_client.list_objects_v2(Bucket=BUCKET)["KeyCount"] == 0


def test_prefix_is_applied_and_cannot_be_walked(storage: S3BlobStorage) -> None:
    key = key_for(b"prefixed")
    assert storage.object_key(key) == f"tenderbase/{key}"

    storage.write_bytes(key, b"prefixed")
    listed = [entry["Key"] for entry in storage._client.list_objects_v2(Bucket=BUCKET)["Contents"]]
    assert listed == [f"tenderbase/{key}"]

    unprefixed = S3BlobStorage(s3_settings(s3_key_prefix=""))
    assert unprefixed.object_key(key) == key


def test_keys_are_built_from_digests_only() -> None:
    """A filename from a source website can never reach an object key.

    ``build_storage_key`` keeps hex and a short alphanumeric suffix, so a hostile
    ``Content-Disposition`` such as ``../../etc/passwd`` cannot influence the path.
    """
    digest = sha256_text("payload")
    key = build_storage_key(namespace="documents", digest=digest, extension="../../.exe")
    assert key.startswith("documents/")
    assert key.endswith(".exe"), "the extension is sanitised, not rejected with the name"
    assert ".." not in key
    assert validate_object_key(key) == key
    # A digest that is not a digest is refused rather than embedded.
    with pytest.raises(DocumentError):
        build_storage_key(namespace="documents", digest="../../etc/passwd")


# ---------------------------------------------------------------------------
# Failure modes and bucket assumptions
# ---------------------------------------------------------------------------


def test_a_missing_object_is_absence_but_a_missing_bucket_is_an_error(
    storage: S3BlobStorage, s3_client: Any
) -> None:
    """The distinction that keeps a bucket outage from looking like data loss.

    "No such key" is absence: the downloader should store the document. "No such bucket"
    is a configuration fault, and answering ``False`` to it would tell the downloader to
    re-store an object that is there, and tell an operator their evidence was deleted.
    """
    missing = key_for(b"never written")
    assert storage.exists(missing) is False
    with pytest.raises(DocumentError, match="Object not found"):
        storage.read_bytes(missing)

    s3_client.delete_bucket(Bucket=BUCKET)
    for call in (lambda: storage.exists(missing), lambda: storage.read_bytes(missing)):
        with pytest.raises(DocumentError, match="does not exist; check S3_BUCKET"):
            call()


def test_no_acl_is_ever_sent(storage: S3BlobStorage, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here can make an object public, because nothing sends an ACL."""
    seen: list[dict[str, Any]] = []
    original = storage._client.put_object

    def spy(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(storage._client, "put_object", spy)
    payload = b"private by default"
    storage.write_bytes(key_for(payload), payload)

    assert seen, "the spy never saw a write"
    assert all("ACL" not in call for call in seen)
    assert all(call.get("ServerSideEncryption") == "AES256" for call in seen)


def test_encryption_header_can_be_turned_off_for_a_policy_managed_bucket(
    s3_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    storage = S3BlobStorage(s3_settings(s3_server_side_encryption=""))
    original = storage._client.put_object
    monkeypatch.setattr(
        storage._client,
        "put_object",
        lambda **kwargs: (calls.append(kwargs), original(**kwargs))[1],
    )
    payload = b"no sse header"
    storage.write_bytes(key_for(payload), payload)
    assert "ServerSideEncryption" not in calls[0]


def test_presigned_urls_are_off_until_the_deployment_enables_them(
    storage: S3BlobStorage,
) -> None:
    payload = b"signed?"
    key = key_for(payload)
    storage.write_bytes(key, payload)
    with pytest.raises(DocumentError, match="disabled"):
        storage.presigned_get_url(key)

    enabled = S3BlobStorage(
        s3_settings(s3_presigned_urls_enabled=True, s3_presigned_url_seconds=60)
    )
    url = enabled.presigned_get_url(key)
    assert url.startswith("https://")
    assert "X-Amz-Signature" in url and "X-Amz-Expires=60" in url


# ---------------------------------------------------------------------------
# Configuration and the factories
# ---------------------------------------------------------------------------


def test_the_backend_requires_a_bucket_at_startup() -> None:
    """The app refuses to start with a blob store that has nowhere to write.

    Otherwise the first document download of the deployment is where the discovery
    happens, at 2 a.m., inside a batch that has already been retried twice.
    """
    with pytest.raises(ValidationError, match="S3_BUCKET"):
        Settings(app_env="test", document_storage_backend="s3", s3_bucket="")


@pytest.mark.parametrize(
    "prefix", ["../escape", "bad prefix", "ok/nested/prefix", "dots.not.allowed", ""]
)
def test_key_prefix_is_validated_as_part_of_every_key(prefix: str) -> None:
    if prefix in ("../escape", "bad prefix", "dots.not.allowed"):
        with pytest.raises(ValidationError, match="S3_KEY_PREFIX"):
            s3_settings(s3_key_prefix=prefix)
    else:
        s3_settings(s3_key_prefix=prefix)


def test_half_a_credential_pair_is_refused() -> None:
    """One of the two would be silently ignored by the ambient chain."""
    with pytest.raises(ValidationError, match="must be set together"):
        s3_settings(s3_access_key_id="AKIATESTFIXTURE")
    with pytest.raises(ValidationError, match="must be set together"):
        s3_settings(s3_secret_access_key="secret-only")


def test_missing_boto3_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure message says what to install, instead of an ImportError from a route."""

    class Blocker:
        def find_spec(self, name: str, path: object = None, target: object = None) -> Any:
            if name in {"boto3", "botocore", "botocore.config"}:
                raise ImportError(f"no module named {name}")
            return None

    monkeypatch.setitem(sys.modules, "boto3", None)
    monkeypatch.setattr("botocore.config.Config", Exception, raising=False)
    with pytest.raises(DocumentError, match=r"pip install tenderbase-api\[s3\]"):
        S3BlobStorage(s3_settings())


def test_factories_honour_the_configured_backend(tmp_path: Any, s3_client: Any) -> None:
    local = s3_settings(document_storage_backend="local", document_storage_path=tmp_path)
    assert isinstance(get_document_storage(local), LocalBlobStorage)
    assert isinstance(get_raw_storage(local), LocalBlobStorage)

    remote = s3_settings()
    assert isinstance(get_document_storage(remote), S3BlobStorage)
    # The raw payload is the evidence behind a record: both stores follow one switch so
    # a deployment cannot end up with documents in S3 and evidence on a scratch disk.
    assert isinstance(get_raw_storage(remote), S3BlobStorage)


def test_raw_payload_offload_works_against_s3(s3_client: Any) -> None:
    """The pipeline's evidence offload goes through the same store, unchanged.

    ``store_raw_payload`` is called with a *content hash* and nothing else, which is why
    the S3 backend needs no new concept of identity: the same HTML from two sources
    occupies one object, and the row keeps the key that names it.
    """
    settings = s3_settings()
    payload = "<html>" + ("evidence" * 200) + "</html>"
    key = store_raw_payload(payload, namespace="html", settings=settings)
    assert key.startswith("html/")
    assert key.endswith(".txt")
    stored = S3BlobStorage(settings)
    assert stored.read_bytes(key) == payload.encode("utf-8")
    # Idempotent: the second call must not pay for a second PUT.
    assert store_raw_payload(payload, namespace="html", settings=settings) == key


def test_the_digest_is_the_identity() -> None:
    """Two runs over the same bytes must agree on the key, or dedupe silently fails."""
    data = b"deterministic"
    assert sha256_bytes(data) == sha256_bytes(data)
    assert key_for(data) == key_for(bytearray(data))
    assert key_for(data) != key_for(data + b"!")
    assert key_for(data).endswith(".pdf")


def test_object_keys_use_the_digest_and_stay_under_the_namespace() -> None:
    digest = sha256_text("payload")
    key = build_storage_key(namespace="documents", digest=digest, extension="pdf")
    parts = key.split("/")
    assert parts[0] == "documents"
    assert parts[1] == digest[:2] and parts[2] == digest[2:4]
    assert parts[3] == f"{digest}.pdf"


# ---------------------------------------------------------------------------
# Backend parity: the abstraction is the point
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["local", "s3"])
def test_the_two_backends_answer_identically(tmp_path: Any, s3_client: Any, backend: str) -> None:
    """Same calls, same answers — which is what "switching is configuration" means.

    Every row of this table is a behaviour some caller depends on: the downloader checks
    ``exists`` before writing, the document service reads bytes for extraction, and a
    missing object has to look the same on either side or a deployment that switches
    backends changes the API's answers.
    """
    storage = (
        LocalBlobStorage(tmp_path / "blobs") if backend == "local" else S3BlobStorage(s3_settings())
    )
    payload = b"parity payload"
    key = key_for(payload)

    assert storage.exists(key) is False
    assert storage.size(key) == 0

    stored = storage.write_bytes(key, payload)
    assert stored.backend == backend
    assert stored.size == len(payload)
    assert storage.exists(key) is True
    assert storage.size(key) == len(payload)
    assert storage.read_bytes(key) == payload
    with storage.open_read(key) as reader:
        assert reader.read() == payload

    storage.delete(key)
    assert storage.exists(key) is False
    with pytest.raises(DocumentError, match="Object not found"):
        storage.read_bytes(key)
    with pytest.raises(DocumentError, match="Object not found"):
        with storage.open_read(key):
            pass

    # Both refuse a traversal-shaped key before touching storage.
    with pytest.raises(DocumentError):
        storage.write_bytes("../escape.pdf", payload)


@pytest.mark.parametrize("backend", ["local", "s3"])
def test_streaming_write_is_identical_on_both(tmp_path: Any, s3_client: Any, backend: str) -> None:
    storage = (
        LocalBlobStorage(tmp_path / "stream")
        if backend == "local"
        else S3BlobStorage(s3_settings())
    )
    key = key_for(b"streamed the same")
    with storage.open_write(key) as stream:
        stream.write(b"streamed ")
        stream.write(b"the same")
    assert storage.read_bytes(key) == b"streamed the same"
