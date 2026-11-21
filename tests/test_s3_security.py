"""Regression tests for the S3 backend: signing scope, ACLs, errors, limits."""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import pytest

pytest.importorskip("boto3")
pytest.importorskip("moto")
pytest.importorskip("cryptography", reason="cryptography required for CloudFront")

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from moto import mock_aws  # noqa: E402

from granite_assets.enums import AssetVisibility  # noqa: E402
from granite_assets.exceptions import (  # noqa: E402
    AssetConfigurationError,
    AssetError,
    AssetNotFoundError,
)
from granite_assets.models import (  # noqa: E402
    AssetSaveRequest,
    S3AssetRepositoryConfig,
)
from granite_assets.repositories.s3 import S3AssetRepository  # noqa: E402

BUCKET = "sec-bucket"
REGION = "eu-west-1"
PUB = AssetVisibility.PUBLIC
PRIV = AssetVisibility.PRIVATE


@pytest.fixture()
def pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture(autouse=True)
def _aws(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(k, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


@pytest.fixture()
def bucket() -> Any:
    with mock_aws():
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        yield


def make(pem: str | None = None, **kw: Any) -> S3AssetRepository:
    cfg = S3AssetRepositoryConfig(
        bucket=BUCKET,
        region=REGION,
        public_base_url="https://cdn.example.com",
        cf_key_id="K1" if pem else None,
        cf_private_key=pem,
        **kw,
    )
    return S3AssetRepository(cfg)


def policy_of(url: str) -> dict[str, Any]:
    q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    std = q["Policy"].replace("-", "+").replace("_", "=").replace("~", "/")
    return json.loads(base64.b64decode(std))  # type: ignore[no-any-return]


def resource(url: str) -> str:
    return str(policy_of(url)["Statement"][0]["Resource"])


# ---- GA-005 ------------------------------------------------------------------


def test_download_url_policy_covers_only_the_object(pem: str) -> None:
    repo = make(pem)
    url = repo.build_download_url("invoices/2024/inv-1.pdf").url
    assert resource(url) == "https://cdn.example.com/invoices/2024/inv-1.pdf"
    assert "*" not in resource(url)


def test_download_url_for_root_level_key_is_valid(pem: str) -> None:
    url = make(pem).build_download_url("file.pdf").url
    assert url.startswith("https://cdn.example.com/file.pdf?Policy=")
    assert resource(url) == "https://cdn.example.com/file.pdf"


@pytest.mark.parametrize("pattern", ["*", "/*", "**", "a/*/b", "../x/*", "a/../*"])
def test_path_pattern_rejected(pem: str, pattern: str) -> None:
    repo = make(pem)
    with pytest.raises(AssetError):
        repo.build_path_signed_url("a/b.txt", path_pattern=pattern)
    with pytest.raises(AssetError):
        repo.build_signed_cookies(pattern)


def test_path_pattern_must_cover_the_key(pem: str) -> None:
    repo = make(pem)
    with pytest.raises(AssetError, match="does not cover"):
        repo.build_path_signed_url("a/b.txt", path_pattern="other/*")
    ok = repo.build_path_signed_url("a/b.txt", path_pattern="a/*")
    assert resource(ok.url) == "https://cdn.example.com/a/*"


def test_path_signed_url_root_key_without_pattern_is_not_distribution_wide(
    pem: str,
) -> None:
    url = make(pem).build_path_signed_url("file.m3u8").url
    assert resource(url) == "https://cdn.example.com/file.m3u8"


def test_key_prefix_applies_to_pattern(pem: str) -> None:
    repo = make(pem, key_prefix="p")
    url = repo.build_path_signed_url("a/b.txt", path_pattern="a/*").url
    assert resource(url) == "https://cdn.example.com/p/a/*"


@pytest.mark.parametrize("name", ["", "../x", "a/b", ".."])
def test_entry_filename_validated(pem: str, name: str) -> None:
    with pytest.raises(AssetError):
        make(pem).build_folder_signed_url("a/b.mp4", entry_filename=name)


# ---- GA-006 ------------------------------------------------------------------


def test_cloudfront_urls_are_percent_encoded(pem: str) -> None:
    repo = make(pem)
    url = repo.build_download_url("sp/a b?x=1.txt").url
    path = url.split("?Policy=")[0]
    assert path == "https://cdn.example.com/sp/a%20b%3Fx%3D1.txt"
    assert url.count("?") == 1
    assert resource(url) == path
    assert repo.build_public_url("sp/a b.txt").url == (
        "https://cdn.example.com/sp/a%20b.txt"
    )


# ---- GA-019 ------------------------------------------------------------------


def test_public_api_and_private_aliases(pem: str) -> None:
    repo = make(pem, key_prefix="pre/")
    assert repo.cf_base_url() == repo._cf_base_url() == "https://cdn.example.com"
    assert repo.s3_key("a.txt") == repo._s3_key("a.txt") == "pre/a.txt"
    assert repo.cf_object_url("a b.txt") == "https://cdn.example.com/pre/a%20b.txt"
    with pytest.raises(AssetConfigurationError):
        S3AssetRepository(
            S3AssetRepositoryConfig(bucket=BUCKET, region=REGION)
        ).cf_base_url()


# ---- GA-009 ------------------------------------------------------------------


def test_copy_preserves_public_acl_and_visibility(bucket: Any) -> None:
    repo = make()
    repo.save(
        AssetSaveRequest(
            source=b"x", content_type="text/plain", key="a.txt", visibility=PUB
        )
    )
    repo.copy("a.txt", "b.txt")
    client = boto3.client("s3", region_name=REGION)
    grants = client.get_object_acl(Bucket=BUCKET, Key="b.txt")["Grants"]
    assert any(
        g["Permission"] == "READ" and "AllUsers" in g["Grantee"].get("URI", "")
        for g in grants
    )
    desc = repo.get_descriptor("b.txt")
    assert desc.visibility == PUB and desc.content_type == "text/plain"


def test_copy_private_stays_private_and_overwrites_public_dest(bucket: Any) -> None:
    repo = make()
    repo.save(
        AssetSaveRequest(
            source=b"p", content_type="text/plain", key="pub.txt", visibility=PUB
        )
    )
    repo.save(AssetSaveRequest(source=b"s", content_type="text/plain", key="sec.txt"))
    repo.copy("sec.txt", "pub.txt")
    grants = boto3.client("s3", region_name=REGION).get_object_acl(
        Bucket=BUCKET, Key="pub.txt"
    )["Grants"]
    assert not any("AllUsers" in g["Grantee"].get("URI", "") for g in grants)


def test_copy_and_move_same_key_do_not_destroy_asset(bucket: Any) -> None:
    repo = make()
    repo.save(AssetSaveRequest(source=b"x", content_type="text/plain", key="a.txt"))
    repo.copy("a.txt", "a.txt")
    repo.move("a.txt", "a.txt")
    assert repo.exists("a.txt")
    with pytest.raises(AssetNotFoundError):
        repo.move("nope.txt", "nope.txt")


def test_copy_missing_source_raises_not_found(bucket: Any) -> None:
    with pytest.raises(AssetNotFoundError):
        make().copy("nope.txt", "b.txt")


# ---- GA-011 ------------------------------------------------------------------


class _FakeS3:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def head_object(self, **_: Any) -> Any:
        raise self.exc


def client_error(code: str, status: int, msg: str = "boom") -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": msg},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "HeadObject",
    )


def repo_with(exc: Exception) -> S3AssetRepository:
    repo = make()
    repo._s3 = _FakeS3(exc)
    return repo


def test_exists_does_not_treat_404_in_message_as_missing() -> None:
    err = client_error("AccessDenied", 403, "endpoint https://h:8404/x failed 404")
    with pytest.raises(AssetError):
        repo_with(err).exists("a.txt")
    with pytest.raises(AssetError):
        repo_with(RuntimeError("connection to host404 lost")).exists("a.txt")


def test_exists_and_descriptor_use_structured_codes() -> None:
    assert repo_with(client_error("404", 404)).exists("a.txt") is False
    assert repo_with(client_error("NoSuchKey", 404)).exists("a.txt") is False
    with pytest.raises(AssetNotFoundError):
        repo_with(client_error("404", 404)).get_descriptor("a.txt")
    with pytest.raises(AssetError) as ei:
        repo_with(client_error("AccessDenied", 403, "Not Found?")).get_descriptor("a")
    assert not isinstance(ei.value, AssetNotFoundError)


# ---- GA-008 / overwrite -------------------------------------------------------


def test_save_overwrite_false_in_s3(bucket: Any) -> None:
    repo = make()
    repo.save(AssetSaveRequest(source=b"1", content_type="text/plain", key="a.txt"))
    with pytest.raises(AssetError, match="overwrite"):
        repo.save(
            AssetSaveRequest(
                source=b"2", content_type="text/plain", key="a.txt", overwrite=False
            )
        )
    obj = boto3.client("s3", region_name=REGION).get_object(Bucket=BUCKET, Key="a.txt")
    assert obj["Body"].read() == b"1"


def test_s3_limits(bucket: Any) -> None:
    repo = make(max_upload_bytes=5, allowed_content_types=("text/plain",))
    with pytest.raises(AssetError, match="not allowed"):
        repo.save(AssetSaveRequest(source=b"x", content_type="text/html", key="a.html"))
    with pytest.raises(AssetError, match="maximum"):
        repo.save(
            AssetSaveRequest(
                source=io.BytesIO(b"x" * 6), content_type="text/plain", key="a.txt"
            )
        )
    with pytest.raises(AssetError, match="maximum"):
        repo.save(
            AssetSaveRequest(
                source=b"x" * 6,
                content_type="text/plain",
                key="b.txt",
                content_length=6,
            )
        )
    assert not repo.exists("a.txt") and not repo.exists("b.txt")
    repo.save(AssetSaveRequest(source=b"x" * 5, content_type="text/plain", key="c.txt"))
    with pytest.raises(AssetError):
        repo.build_upload_url("a.html", "text/html")
    with pytest.raises(AssetError, match="maximum"):
        repo.build_upload_url("a.txt", "text/plain", content_length=6)


def test_upload_url_binds_content_length(bucket: Any) -> None:
    repo = make()
    res = repo.build_upload_url("a.txt", "text/plain", content_length=10)
    assert res.headers["Content-Length"] == "10"
    assert "content-length" in res.url.lower().split("signedheaders=")[1]
    plain = repo.build_upload_url("a.txt", "text/plain")
    assert "Content-Length" not in plain.headers


# ---- GA-010 ------------------------------------------------------------------


def test_secrets_not_in_repr(pem: str) -> None:
    from granite_assets.models import LocalNginxAssetRepositoryConfig

    cfg = S3AssetRepositoryConfig(
        bucket="b",
        region="r",
        secret_access_key="SEKRET-AK",
        session_token="SEKRET-ST",
        cf_key_id="K",
        cf_private_key=pem,
    )
    text = repr(cfg)
    assert "SEKRET" not in text and "BEGIN" not in text
    loc = LocalNginxAssetRepositoryConfig(
        storage_path="/s",
        base_url="http://h",
        secure_link_secret="SEKRET-SL",
        upload_secret="SEKRET-UP",
    )
    assert "SEKRET" not in repr(loc)
