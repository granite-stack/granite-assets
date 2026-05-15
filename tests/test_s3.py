"""Tests for S3AssetRepository using moto as a local S3 mock."""

from __future__ import annotations

import pytest

pytest.importorskip("boto3", reason="boto3 not installed (s3 extra required)")
pytest.importorskip("moto", reason="moto not installed (dev extra required)")

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

from granite_assets.enums import AssetVisibility, CfSigningMethod  # noqa: E402
from granite_assets.exceptions import AssetNotFoundError  # noqa: E402
from granite_assets.models import (  # noqa: E402
    AssetSaveRequest,
    CfSignedCookies,
    S3AssetRepositoryConfig,
)
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
def test_save_result_content_length_from_request(
    aws_credentials: None, config: S3AssetRepositoryConfig
) -> None:
    """When content_length is supplied in the request it must be reflected
    in the result."""
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(config)

    payload = b"hello world"
    req = AssetSaveRequest(
        key="files/hello.txt",
        source=payload,
        content_type="text/plain",
        visibility=AssetVisibility.PRIVATE,
        content_length=len(payload),
    )
    result = repo.save(req)

    assert result.content_length == len(payload)


@mock_aws
def test_save_result_content_length_from_head_when_not_in_request(
    aws_credentials: None, config: S3AssetRepositoryConfig
) -> None:
    """When content_length is omitted from the request, the result is obtained
    via head_object."""
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(config)

    payload = b"hello world"
    req = AssetSaveRequest(
        key="files/hello-no-hint.txt",
        source=payload,
        content_type="text/plain",
        visibility=AssetVisibility.PRIVATE,
        # content_length intentionally omitted
    )
    result = repo.save(req)

    assert result.content_length == len(payload)


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

    repo.save(AssetSaveRequest(key="bye.txt", source=b"bye", content_type="text/plain"))
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

    repo.save(
        AssetSaveRequest(key="src.txt", source=b"hello", content_type="text/plain")
    )
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

    repo.save(
        AssetSaveRequest(key="mv-src.txt", source=b"data", content_type="text/plain")
    )
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
        AssetSaveRequest(
            key="private.pdf", source=b"secret", content_type="application/pdf"
        )
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

    repo.save(
        AssetSaveRequest(key="file.txt", source=b"data", content_type="text/plain")
    )
    assert repo.exists("file.txt")

    # Ensure the actual S3 key has the prefix
    response = client.list_objects_v2(Bucket=BUCKET, Prefix="tenant-a/assets/")
    keys = [obj["Key"] for obj in response.get("Contents", [])]
    assert "tenant-a/assets/file.txt" in keys


# ---------------------------------------------------------------------------
# CloudFront signing — fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_rsa_pem() -> str:
    """Generate a throwaway 1024-bit RSA key for signing tests (speed over security)."""
    pytest.importorskip("cryptography", reason="cryptography not installed")
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(
        public_exponent=65537, key_size=1024, backend=default_backend()
    )
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture()
def cf_url_config(test_rsa_pem: str) -> S3AssetRepositoryConfig:
    """Config with CloudFront signing enabled in URL mode (default)."""
    return S3AssetRepositoryConfig(
        bucket=BUCKET,
        region=REGION,
        public_base_url="https://cdn.example.com",
        presign_ttl_seconds=3600,
        cf_key_id="TESTKEY123",
        cf_private_key=test_rsa_pem,
        cf_signing_method=CfSigningMethod.URL,
    )


@pytest.fixture()
def cf_cookie_config(test_rsa_pem: str) -> S3AssetRepositoryConfig:
    """Config with CloudFront signing enabled in COOKIE mode."""
    return S3AssetRepositoryConfig(
        bucket=BUCKET,
        region=REGION,
        public_base_url="https://cdn.example.com",
        presign_ttl_seconds=3600,
        cf_key_id="TESTKEY123",
        cf_private_key=test_rsa_pem,
        cf_signing_method=CfSigningMethod.COOKIE,
    )


# ---------------------------------------------------------------------------
# Auto-generated key: <uuid>/<uuid>.<ext>
# ---------------------------------------------------------------------------


@mock_aws
def test_save_without_key_generates_uuid_folder_structure(
    aws_credentials: None, config: S3AssetRepositoryConfig
) -> None:
    """When key is omitted, save() must store the file at <uuid>/<uuid>.<ext>."""
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(config)

    result = repo.save(
        AssetSaveRequest(source=b"data", content_type="image/png", filename="photo.png")
    )

    parts = result.key.split("/")
    assert len(parts) == 2, f"Expected <uuid>/<uuid>.ext, got {result.key!r}"
    folder, filename_with_ext = parts
    stem, ext = filename_with_ext.rsplit(".", 1)
    assert folder == stem, "Folder UUID must match filename UUID"
    assert ext == "png"


@mock_aws
def test_save_without_key_asset_is_retrievable(
    aws_credentials: None, config: S3AssetRepositoryConfig
) -> None:
    """The auto-generated key must actually be retrievable from S3."""
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(config)

    result = repo.save(
        AssetSaveRequest(
            source=b"hello", content_type="text/plain", filename="note.txt"
        )
    )

    assert repo.exists(result.key)


@mock_aws
def test_save_without_key_no_extension(
    aws_credentials: None, config: S3AssetRepositoryConfig
) -> None:
    """A file with no extension produces <uuid>/<uuid> (no trailing dot)."""
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(config)

    result = repo.save(
        AssetSaveRequest(source=b"raw", content_type="application/octet-stream")
    )

    parts = result.key.split("/")
    assert len(parts) == 2
    assert parts[0] == parts[1], (
        "Folder and filename UUIDs must match when no extension"
    )


@mock_aws
def test_save_with_explicit_key_uses_it_unchanged(
    aws_credentials: None, config: S3AssetRepositoryConfig
) -> None:
    """When key is provided explicitly it must be used as-is (backward compat)."""
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(config)

    result = repo.save(
        AssetSaveRequest(
            source=b"data", content_type="text/plain", key="custom/path/file.txt"
        )
    )

    assert result.key == "custom/path/file.txt"


# ---------------------------------------------------------------------------
# CloudFront signing — build_path_signed_url (custom policy)
# ---------------------------------------------------------------------------


@mock_aws
def test_build_path_signed_url_contains_policy_param(
    aws_credentials: None, cf_url_config: S3AssetRepositoryConfig
) -> None:
    """Custom-policy URL must contain Policy= and Signature= (not Expires=)."""
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(cf_url_config)

    result = repo.build_path_signed_url("private/videos/abc123/master.m3u8")
    assert "Policy=" in result.url
    assert "Signature=" in result.url
    assert "Key-Pair-Id=TESTKEY123" in result.url
    # Canned-policy param must NOT appear
    assert "Expires=" not in result.url
    assert result.expires_at is not None


@mock_aws
def test_build_path_signed_url_wildcard_resource_derived(
    aws_credentials: None, cf_url_config: S3AssetRepositoryConfig
) -> None:
    """When no path_pattern is given, resource in policy covers the directory
    with a wildcard."""
    import base64
    import json

    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(cf_url_config)

    result = repo.build_path_signed_url("private/videos/abc123/master.m3u8")

    # Extract Policy= param and decode it
    params = dict(part.split("=", 1) for part in result.url.split("?", 1)[1].split("&"))
    policy_b64 = params["Policy"]
    # Reverse CloudFront base64 encoding
    policy_b64_standard = (
        policy_b64.replace("-", "+").replace("_", "=").replace("~", "/")
    )
    policy = json.loads(base64.b64decode(policy_b64_standard + "=="))

    resource = policy["Statement"][0]["Resource"]
    assert resource.endswith("/*"), f"Resource should end with /*, got: {resource!r}"
    assert "private/videos/abc123/" in resource


@mock_aws
def test_build_path_signed_url_explicit_pattern(
    aws_credentials: None, cf_url_config: S3AssetRepositoryConfig
) -> None:
    """Explicit path_pattern is used verbatim in the signed policy."""
    import base64
    import json

    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(cf_url_config)

    result = repo.build_path_signed_url(
        "private/videos/abc123/master.m3u8",
        path_pattern="private/videos/abc123/*",
    )
    params = dict(part.split("=", 1) for part in result.url.split("?", 1)[1].split("&"))
    policy_b64_standard = (
        params["Policy"].replace("-", "+").replace("_", "=").replace("~", "/")
    )
    policy = json.loads(base64.b64decode(policy_b64_standard + "=="))

    resource = policy["Statement"][0]["Resource"]
    assert "private/videos/abc123/*" in resource


# ---------------------------------------------------------------------------
# CloudFront signing — build_signed_cookies
# ---------------------------------------------------------------------------


@mock_aws
def test_build_signed_cookies_returns_three_values(
    aws_credentials: None, cf_url_config: S3AssetRepositoryConfig
) -> None:
    """build_signed_cookies returns a CfSignedCookies with non-empty values."""
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(cf_url_config)

    cookies = repo.build_signed_cookies("private/videos/abc123/*")

    assert isinstance(cookies, CfSignedCookies)
    assert cookies.policy
    assert cookies.signature
    assert cookies.key_pair_id == "TESTKEY123"
    assert cookies.expires_at is not None


@mock_aws
def test_build_signed_cookies_as_cookie_header_values(
    aws_credentials: None, cf_url_config: S3AssetRepositoryConfig
) -> None:
    """as_cookie_header_values returns the three required CF cookie names."""
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(cf_url_config)

    cookies = repo.build_signed_cookies("private/videos/abc123/*")
    header_values = cookies.as_cookie_header_values()

    assert "CloudFront-Policy" in header_values
    assert "CloudFront-Signature" in header_values
    assert "CloudFront-Key-Pair-Id" in header_values
    assert header_values["CloudFront-Key-Pair-Id"] == "TESTKEY123"


# ---------------------------------------------------------------------------
# CloudFront signing — cookie mode in build_download_url
# ---------------------------------------------------------------------------


@mock_aws
def test_build_download_url_cookie_mode_returns_plain_cf_url(
    aws_credentials: None, cf_cookie_config: S3AssetRepositoryConfig
) -> None:
    """In COOKIE mode, build_download_url returns a plain CF URL (no signature
    params)."""
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(cf_cookie_config)
    repo.save(
        AssetSaveRequest(
            key="private/video.mp4", source=b"data", content_type="video/mp4"
        )
    )

    result = repo.build_download_url("private/video.mp4")

    assert result.url.startswith("https://cdn.example.com/")
    assert "Signature" not in result.url
    assert "Policy" not in result.url
    assert "Expires" not in result.url
    # Plain URL — no expiry (cookies carry the expiry)
    assert result.expires_at is None


@mock_aws
def test_build_download_url_url_mode_returns_signed_url(
    aws_credentials: None, cf_url_config: S3AssetRepositoryConfig
) -> None:
    """In URL mode (default), build_download_url returns a CloudFront signed URL."""
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(cf_url_config)
    repo.save(
        AssetSaveRequest(
            key="private/doc.pdf", source=b"data", content_type="application/pdf"
        )
    )

    result = repo.build_download_url("private/doc.pdf")

    assert "Policy=" in result.url
    assert "Signature=" in result.url
    assert "Key-Pair-Id=" in result.url
    assert "Expires=" not in result.url  # custom policy, not canned
    assert result.expires_at is not None


# ---------------------------------------------------------------------------
# CloudFront signing — build_folder_signed_url
# ---------------------------------------------------------------------------

_ASSET_UUID = "550e8400-e29b-41d4-a716-446655440000"
_ASSET_KEY = f"assets/{_ASSET_UUID}/{_ASSET_UUID}.mp4"


@mock_aws
def test_build_folder_signed_url_points_to_entry_file(
    aws_credentials: None, cf_url_config: S3AssetRepositoryConfig
) -> None:
    """URL must point to <folder>/<entry_filename>, not to the original key."""
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(cf_url_config)

    result = repo.build_folder_signed_url(_ASSET_KEY, entry_filename="master.m3u8")

    # Base URL must contain the entry filename, not the source .mp4
    assert "master.m3u8" in result.url
    assert f"{_ASSET_UUID}.mp4" not in result.url.split("?")[0]
    assert result.expires_at is not None


@mock_aws
def test_build_folder_signed_url_resource_is_wildcard(
    aws_credentials: None, cf_url_config: S3AssetRepositoryConfig
) -> None:
    """The policy Resource must be <cf_base>/<folder>/* (wildcard over the folder)."""
    import base64
    import json

    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(cf_url_config)

    result = repo.build_folder_signed_url(_ASSET_KEY, entry_filename="master.m3u8")

    params = dict(part.split("=", 1) for part in result.url.split("?", 1)[1].split("&"))
    policy_b64_standard = (
        params["Policy"].replace("-", "+").replace("_", "=").replace("~", "/")
    )
    policy = json.loads(base64.b64decode(policy_b64_standard + "=="))

    resource = policy["Statement"][0]["Resource"]
    expected_folder = f"assets/{_ASSET_UUID}"
    assert resource.endswith("/*"), f"Resource should end with /*, got {resource!r}"
    assert expected_folder in resource, (
        f"Resource should contain folder, got {resource!r}"
    )
    # Must NOT reference the specific .mp4 file
    assert f"{_ASSET_UUID}.mp4" not in resource


@mock_aws
def test_build_folder_signed_url_has_cf_params(
    aws_credentials: None, cf_url_config: S3AssetRepositoryConfig
) -> None:
    """URL must carry Policy=, Signature= and Key-Pair-Id= query params."""
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(cf_url_config)

    result = repo.build_folder_signed_url(_ASSET_KEY, entry_filename="master.m3u8")

    assert "Policy=" in result.url
    assert "Signature=" in result.url
    assert "Key-Pair-Id=TESTKEY123" in result.url
    assert "Expires=" not in result.url  # custom policy, not canned


@mock_aws
def test_build_folder_signed_url_with_key_prefix(
    aws_credentials: None, test_rsa_pem: str
) -> None:
    """key_prefix is included in both the S3 key and the CF resource pattern."""
    import base64
    import json

    cfg = S3AssetRepositoryConfig(
        bucket=BUCKET,
        region=REGION,
        public_base_url="https://cdn.example.com",
        key_prefix="prod",
        cf_key_id="TESTKEY123",
        cf_private_key=test_rsa_pem,
    )
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(cfg)

    result = repo.build_folder_signed_url(_ASSET_KEY, entry_filename="master.m3u8")

    # The URL path should include the key_prefix
    url_path = result.url.split("?")[0]
    assert "prod/" in url_path

    # The resource pattern in the policy should also include the key_prefix
    params = dict(part.split("=", 1) for part in result.url.split("?", 1)[1].split("&"))
    policy_b64_standard = (
        params["Policy"].replace("-", "+").replace("_", "=").replace("~", "/")
    )
    policy = json.loads(base64.b64decode(policy_b64_standard + "=="))
    resource = policy["Statement"][0]["Resource"]
    assert "prod/" in resource


@mock_aws
def test_build_folder_signed_url_root_key_raises(
    aws_credentials: None, cf_url_config: S3AssetRepositoryConfig
) -> None:
    """A root-level key (no '/') should raise AssetError."""
    from granite_assets.exceptions import AssetError

    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(cf_url_config)

    with pytest.raises(AssetError, match="Cannot derive folder"):
        repo.build_folder_signed_url("rootfile.mp4", entry_filename="master.m3u8")


@mock_aws
def test_build_folder_signed_url_requires_cf_config(
    aws_credentials: None, config: S3AssetRepositoryConfig
) -> None:
    """Raises AssetConfigurationError when CF signing is not configured."""
    from granite_assets.exceptions import AssetConfigurationError

    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    repo = S3AssetRepository(config)  # no cf_key_id / cf_private_key

    with pytest.raises(AssetConfigurationError):
        repo.build_folder_signed_url(_ASSET_KEY, entry_filename="master.m3u8")
