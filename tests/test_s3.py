"""Tests for S3AssetRepository using moto as a local S3 mock."""

from __future__ import annotations

import pytest

pytest.importorskip("boto3", reason="boto3 not installed (s3 extra required)")
pytest.importorskip("moto", reason="moto not installed (dev extra required)")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

from granite_assets.enums import AssetVisibility  # noqa: E402
from granite_assets.exceptions import AssetNotFoundError  # noqa: E402
from granite_assets.models import AssetSaveRequest, S3AssetRepositoryConfig  # noqa: E402
from granite_assets.repositories.s3 import S3AssetRepository  # noqa: E402

BUCKET = "test-granite-assets"
REGION = "eu-west-1"


@pytest.fixture()
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide fake AWS credentials to prevent accidental real calls."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


@pytest.fixture()
def s3_bucket(aws_credentials: None) -> None:  # type: ignore[misc]
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        yield


@pytest.fixture()
def config() -> S3AssetRepositoryConfig:
    return S3AssetRepositoryConfig(
        bucket=BUCKET,
        region=REGION,
        public_base_url="https://cdn.example.com",
        presign_ttl_seconds=3600,
    )


@pytest.fixture()
def repo(s3_bucket: None, config: S3AssetRepositoryConfig) -> S3AssetRepository:  # type: ignore[misc]
    return S3AssetRepository(config)


# ---------------------------------------------------------------------------
# save / exists
# ---------------------------------------------------------------------------


@mock_aws
def test_save_and_exists(
    aws_credentials: None, config: S3AssetRepositoryConfig
) -> None:
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(config)

    req = AssetSaveRequest(
        key="documents/test.pdf",
        source=b"%PDF-1.4",
        content_type="application/pdf",
        visibility=AssetVisibility.PRIVATE,
    )
    result = repo.save(req)

    assert result.key == "documents/test.pdf"
    assert result.visibility == AssetVisibility.PRIVATE
    assert "s3://" in result.backend_ref
    assert repo.exists("documents/test.pdf") is True


@mock_aws
def test_save_public_asset(
    aws_credentials: None, config: S3AssetRepositoryConfig
) -> None:
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(config)

    req = AssetSaveRequest(
        key="images/logo.png",
        source=b"\x89PNG",
        content_type="image/png",
        visibility=AssetVisibility.PUBLIC,
    )
    result = repo.save(req)
    assert result.visibility == AssetVisibility.PUBLIC


@mock_aws
def test_exists_false_for_missing(
    aws_credentials: None, config: S3AssetRepositoryConfig
) -> None:
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(config)
    assert repo.exists("not/there.txt") is False


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@mock_aws
def test_delete(aws_credentials: None, config: S3AssetRepositoryConfig) -> None:
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(config)

    repo.save(
        AssetSaveRequest(key="bye.txt", source=b"bye", content_type="text/plain")
    )
    assert repo.exists("bye.txt")

    repo.delete("bye.txt")
    assert not repo.exists("bye.txt")


@mock_aws
def test_delete_missing_raises(
    aws_credentials: None, config: S3AssetRepositoryConfig
) -> None:
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(config)

    with pytest.raises(AssetNotFoundError):
        repo.delete("ghost.txt")


# ---------------------------------------------------------------------------
# copy / move
# ---------------------------------------------------------------------------


@mock_aws
def test_copy(aws_credentials: None, config: S3AssetRepositoryConfig) -> None:
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(config)

    repo.save(AssetSaveRequest(key="src.txt", source=b"hello", content_type="text/plain"))
    repo.copy("src.txt", "dst.txt")

    assert repo.exists("src.txt")
    assert repo.exists("dst.txt")


@mock_aws
def test_move(aws_credentials: None, config: S3AssetRepositoryConfig) -> None:
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(config)

    repo.save(AssetSaveRequest(key="mv-src.txt", source=b"data", content_type="text/plain"))
    repo.move("mv-src.txt", "mv-dst.txt")

    assert not repo.exists("mv-src.txt")
    assert repo.exists("mv-dst.txt")


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


@mock_aws
def test_build_public_url_with_cdn(
    aws_credentials: None, config: S3AssetRepositoryConfig
) -> None:
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(config)

    url = repo.build_public_url("images/hero.jpg")
    assert url.url == "https://cdn.example.com/images/hero.jpg"
    assert url.is_permanent is True


@mock_aws
def test_build_public_url_without_cdn(
    aws_credentials: None,
) -> None:
    cfg = S3AssetRepositoryConfig(bucket=BUCKET, region=REGION)
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(cfg)

    url = repo.build_public_url("images/hero.jpg")
    assert "amazonaws.com" in url.url
    assert BUCKET in url.url


@mock_aws
def test_build_download_url(
    aws_credentials: None, config: S3AssetRepositoryConfig
) -> None:
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(config)

    repo.save(
        AssetSaveRequest(key="private.pdf", source=b"secret", content_type="application/pdf")
    )
    access = repo.build_download_url("private.pdf", ttl_seconds=900)
    assert access.url
    assert access.expires_at is not None


@mock_aws
def test_build_upload_url(
    aws_credentials: None, config: S3AssetRepositoryConfig
) -> None:
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(config)

    result = repo.build_upload_url("uploads/photo.jpg", "image/jpeg", ttl_seconds=600)
    assert result.url
    assert result.method == "PUT"
    assert result.headers["Content-Type"] == "image/jpeg"
    assert result.expires_at is not None
    assert result.key == "uploads/photo.jpg"


# ---------------------------------------------------------------------------
# key_prefix support
# ---------------------------------------------------------------------------


@mock_aws
def test_key_prefix(aws_credentials: None) -> None:
    cfg = S3AssetRepositoryConfig(
        bucket=BUCKET, region=REGION, key_prefix="tenant-a/assets"
    )
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(cfg)

    repo.save(AssetSaveRequest(key="file.txt", source=b"data", content_type="text/plain"))
    assert repo.exists("file.txt")

    # Ensure the actual S3 key has the prefix
    response = client.list_objects_v2(Bucket=BUCKET, Prefix="tenant-a/assets/")
    keys = [obj["Key"] for obj in response.get("Contents", [])]
    assert "tenant-a/assets/file.txt" in keys
