"""Integration tests for S3AssetRepository using LocalStack.

These tests spin up a real LocalStack container and verify behaviours that
moto cannot cover because moto intercepts boto3 at the Python layer without
running an actual HTTP server:

* Presigned **download** URLs are consumed by a real HTTP client (httpx GET).
* Presigned **upload** URLs accept a real HTTP PUT from a client (httpx PUT).
* Server-side object storage and retrieval work end-to-end.

Prerequisites
-------------
* Docker must be running on the host.
* No manual setup is required — testcontainers manages the container.

Marks
-----
``pytest.mark.integration`` — skip these when Docker is unavailable::

    pytest -m "not integration"
"""

from __future__ import annotations

import httpx
import pytest

from granite_assets.enums import AssetVisibility
from granite_assets.exceptions import AssetNotFoundError
from granite_assets.models import (
    AssetSaveRequest,
    S3AssetRepositoryConfig,
)
from granite_assets.repositories.s3 import S3AssetRepository

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUCKET = "granite-assets-integration"
REGION = "us-east-1"

# LocalStack credentials — these are fixed dummy values required by AWS SDK.
_LS_KEY = "testcontainers-localstack"
_LS_SECRET = "testcontainers-localstack"

# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def localstack():
    """Start LocalStack with S3 enabled and yield the container."""
    from testcontainers.core.wait_strategies import HttpWaitStrategy
    from testcontainers.localstack import LocalStackContainer

    container = (
        LocalStackContainer(image="localstack/localstack:3", region_name=REGION)
        .with_services("s3")
        # LocalStack exposes a health endpoint — wait until S3 is initialised.
        .waiting_for(HttpWaitStrategy(4566, "/_localstack/health"))
    )
    with container:
        yield container


@pytest.fixture(scope="session")
def s3_bucket(localstack):
    """Create the test bucket once for the whole session."""
    import boto3

    client = boto3.client(
        "s3",
        region_name=REGION,
        endpoint_url=localstack.get_url(),
        aws_access_key_id=_LS_KEY,
        aws_secret_access_key=_LS_SECRET,
    )
    client.create_bucket(Bucket=BUCKET)
    return client


@pytest.fixture(scope="session")
def config(localstack) -> S3AssetRepositoryConfig:
    """Repository config pointing at the LocalStack endpoint."""
    return S3AssetRepositoryConfig(
        bucket=BUCKET,
        region=REGION,
        endpoint_url=localstack.get_url(),
        access_key_id=_LS_KEY,
        secret_access_key=_LS_SECRET,
        presign_ttl_seconds=3600,
    )


@pytest.fixture(scope="session")
def repo(s3_bucket, config: S3AssetRepositoryConfig) -> S3AssetRepository:
    """Shared repository instance for the test session."""
    return S3AssetRepository(config)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _save(
    repo: S3AssetRepository,
    key: str,
    content: bytes = b"hello from integration test",
    content_type: str = "text/plain",
    visibility: AssetVisibility = AssetVisibility.PRIVATE,
) -> None:
    repo.save(
        AssetSaveRequest(
            key=key,
            source=content,
            content_type=content_type,
            visibility=visibility,
        )
    )


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_save_and_exists(repo: S3AssetRepository) -> None:
    _save(repo, "crud/exists.txt")
    assert repo.exists("crud/exists.txt") is True


@pytest.mark.integration
def test_exists_false_for_missing(repo: S3AssetRepository) -> None:
    assert repo.exists("crud/does-not-exist.txt") is False


@pytest.mark.integration
def test_save_returns_correct_metadata(repo: S3AssetRepository) -> None:
    result = repo.save(
        AssetSaveRequest(
            key="crud/meta.pdf",
            source=b"%PDF",
            content_type="application/pdf",
            visibility=AssetVisibility.PRIVATE,
        )
    )
    assert result.key == "crud/meta.pdf"
    assert result.visibility == AssetVisibility.PRIVATE
    assert "s3://" in result.backend_ref
    assert BUCKET in result.backend_ref


@pytest.mark.integration
def test_delete(repo: S3AssetRepository) -> None:
    _save(repo, "crud/delete-me.txt")
    assert repo.exists("crud/delete-me.txt")
    repo.delete("crud/delete-me.txt")
    assert not repo.exists("crud/delete-me.txt")


@pytest.mark.integration
def test_delete_missing_raises(repo: S3AssetRepository) -> None:
    with pytest.raises(AssetNotFoundError):
        repo.delete("crud/ghost.txt")


@pytest.mark.integration
def test_copy(repo: S3AssetRepository) -> None:
    _save(repo, "crud/copy-src.txt")
    repo.copy("crud/copy-src.txt", "crud/copy-dst.txt")
    assert repo.exists("crud/copy-src.txt")
    assert repo.exists("crud/copy-dst.txt")


@pytest.mark.integration
def test_move(repo: S3AssetRepository) -> None:
    _save(repo, "crud/move-src.txt")
    repo.move("crud/move-src.txt", "crud/move-dst.txt")
    assert not repo.exists("crud/move-src.txt")
    assert repo.exists("crud/move-dst.txt")


@pytest.mark.integration
def test_get_descriptor(repo: S3AssetRepository) -> None:
    _save(repo, "crud/descriptor.txt", content=b"payload", content_type="text/plain")
    desc = repo.get_descriptor("crud/descriptor.txt")
    assert desc.key == "crud/descriptor.txt"
    assert desc.content_type == "text/plain"
    assert desc.content_length == len(b"payload")


# ---------------------------------------------------------------------------
# Presigned download URL — the key test moto cannot cover
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_presigned_download_url_is_consumable(repo: S3AssetRepository) -> None:
    """A presigned GET URL generated by the library must return the object body."""
    content = b"secret document content"
    _save(repo, "presign/download.txt", content=content)

    access = repo.build_download_url("presign/download.txt", ttl_seconds=300)

    assert access.expires_at is not None
    assert not access.is_permanent

    response = httpx.get(access.url, timeout=5)
    assert response.status_code == 200
    assert response.content == content


@pytest.mark.integration
def test_presigned_download_url_wrong_key_returns_4xx(
    repo: S3AssetRepository,
    localstack,
) -> None:
    """A presigned URL for a non-existent key must be rejected by the server."""
    import boto3

    # Generate a presigned URL for a key that doesn't exist.
    client = boto3.client(
        "s3",
        region_name=REGION,
        endpoint_url=localstack.get_url(),
        aws_access_key_id=_LS_KEY,
        aws_secret_access_key=_LS_SECRET,
    )
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": "presign/nonexistent.txt"},
        ExpiresIn=60,
    )
    response = httpx.get(url, timeout=5)
    assert response.status_code in (403, 404)


# ---------------------------------------------------------------------------
# Presigned upload URL — client-side PUT flow
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_presigned_upload_url_put_succeeds(repo: S3AssetRepository) -> None:
    """A client must be able to PUT an object via the presigned upload URL."""
    key = "presign/upload-via-put.bin"
    content = b"\x00\x01\x02\x03 binary payload"

    upload = repo.build_upload_url(key, "application/octet-stream", ttl_seconds=300)

    assert upload.method == "PUT"
    assert upload.expires_at is not None

    # Simulate what a browser / mobile client would do.
    response = httpx.put(
        upload.url,
        content=content,
        headers=upload.headers,
        timeout=10,
    )
    assert response.status_code in (200, 204)

    # Verify the object is now visible through the repository.
    assert repo.exists(key)


@pytest.mark.integration
def test_presigned_upload_then_download_roundtrip(repo: S3AssetRepository) -> None:
    """Full roundtrip: upload via presigned PUT, download via presigned GET."""
    key = "presign/roundtrip.txt"
    content = b"roundtrip content - written by client, read via presign"

    upload = repo.build_upload_url(key, "text/plain", ttl_seconds=300)
    put_response = httpx.put(
        upload.url, content=content, headers=upload.headers, timeout=10
    )
    assert put_response.status_code in (200, 204)

    access = repo.build_download_url(key, ttl_seconds=300)
    get_response = httpx.get(access.url, timeout=5)
    assert get_response.status_code == 200
    assert get_response.content == content


# ---------------------------------------------------------------------------
# resolve_access
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_resolve_access_private_returns_presigned(repo: S3AssetRepository) -> None:
    key = "access/private.txt"
    _save(repo, key, visibility=AssetVisibility.PRIVATE)

    access = repo.resolve_access(key)
    assert not access.is_permanent

    response = httpx.get(access.url, timeout=5)
    assert response.status_code == 200


@pytest.mark.integration
def test_resolve_access_missing_raises(repo: S3AssetRepository) -> None:
    with pytest.raises(AssetNotFoundError):
        repo.resolve_access("access/missing.txt")


# ---------------------------------------------------------------------------
# key_prefix transparent mapping
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_key_prefix_transparent(localstack, s3_bucket) -> None:
    """Logical keys are transparent — the prefix is invisible to callers."""
    import boto3

    cfg = S3AssetRepositoryConfig(
        bucket=BUCKET,
        region=REGION,
        endpoint_url=localstack.get_url(),
        access_key_id=_LS_KEY,
        secret_access_key=_LS_SECRET,
        key_prefix="tenant-x/assets",
    )
    prefixed_repo = S3AssetRepository(cfg)

    prefixed_repo.save(
        AssetSaveRequest(key="file.txt", source=b"data", content_type="text/plain")
    )
    assert prefixed_repo.exists("file.txt")

    # Verify the real S3 key carries the prefix.
    client = boto3.client(
        "s3",
        region_name=REGION,
        endpoint_url=localstack.get_url(),
        aws_access_key_id=_LS_KEY,
        aws_secret_access_key=_LS_SECRET,
    )
    response = client.list_objects_v2(Bucket=BUCKET, Prefix="tenant-x/assets/")
    keys = [obj["Key"] for obj in response.get("Contents", [])]
    assert "tenant-x/assets/file.txt" in keys
