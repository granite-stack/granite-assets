"""Tests for domain models and enums."""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest

from granite_assets.enums import AssetVisibility
from granite_assets.models import (
    AssetAccessUrl,
    AssetDescriptor,
    AssetSaveRequest,
    AssetSaveResult,
    LocalNginxAssetRepositoryConfig,
    S3AssetRepositoryConfig,
    UploadUrlResult,
)


class TestAssetVisibility:
    def test_values(self) -> None:
        assert AssetVisibility.PUBLIC == "public"
        assert AssetVisibility.PRIVATE == "private"

    def test_is_str(self) -> None:
        assert isinstance(AssetVisibility.PUBLIC, str)


class TestAssetSaveRequest:
    def test_default_visibility_is_private(self) -> None:
        req = AssetSaveRequest(
            key="test/file.txt",
            source=b"hello",
            content_type="text/plain",
        )
        assert req.visibility == AssetVisibility.PRIVATE

    def test_open_source_bytes(self) -> None:
        req = AssetSaveRequest(key="k", source=b"data", content_type="text/plain")
        stream = req.open_source()
        assert stream.read() == b"data"

    def test_open_source_stream(self) -> None:
        buf = io.BytesIO(b"stream-data")
        req = AssetSaveRequest(key="k", source=buf, content_type="text/plain")
        assert req.open_source() is buf

    def test_metadata_defaults_to_empty_dict(self) -> None:
        req = AssetSaveRequest(key="k", source=b"", content_type="text/plain")
        assert req.metadata == {}

    def test_key_with_path(self) -> None:
        req = AssetSaveRequest(
            key="invoices/2024/inv-001.pdf",
            source=b"pdf",
            content_type="application/pdf",
        )
        assert req.key == "invoices/2024/inv-001.pdf"


class TestAssetAccessUrl:
    def test_permanent_url_has_no_expiry(self) -> None:
        url = AssetAccessUrl(url="https://cdn.example.com/file.jpg")
        assert url.is_permanent is True
        assert url.expires_at is None

    def test_signed_url_is_not_permanent(self) -> None:
        url = AssetAccessUrl(
            url="https://s3.amazonaws.com/...",
            expires_at=datetime.now(tz=timezone.utc),
        )
        assert url.is_permanent is False

    def test_frozen(self) -> None:
        url = AssetAccessUrl(url="https://example.com")
        with pytest.raises(AttributeError):
            url.url = "https://other.com"  # type: ignore[misc]


class TestAssetSaveResult:
    def test_frozen(self) -> None:
        result = AssetSaveResult(key="k", backend_ref="ref")
        with pytest.raises(AttributeError):
            result.key = "other"  # type: ignore[misc]


class TestAssetDescriptor:
    def test_defaults(self) -> None:
        desc = AssetDescriptor(key="test.txt")
        assert desc.content_type is None
        assert desc.content_length is None
        assert desc.metadata == {}
        assert desc.visibility == AssetVisibility.PRIVATE


class TestLocalNginxConfig:
    def test_defaults(self) -> None:
        cfg = LocalNginxAssetRepositoryConfig(
            storage_path="/tmp/assets",
            base_url="http://localhost/assets",
        )
        assert cfg.public_prefix == "public"
        assert cfg.private_prefix == "private"
        assert cfg.overwrite is True
        assert cfg.create_directories is True


class TestS3Config:
    def test_defaults(self) -> None:
        cfg = S3AssetRepositoryConfig(bucket="my-bucket", region="eu-west-1")
        assert cfg.key_prefix == ""
        assert cfg.presign_ttl_seconds == 3600
        assert cfg.endpoint_url is None

    def test_presign_ttl_timedelta(self) -> None:
        cfg = S3AssetRepositoryConfig(bucket="b", region="r", presign_ttl_seconds=7200)
        td = cfg.presign_ttl()
        assert td.seconds == 7200


class TestUploadUrlResult:
    def test_frozen(self) -> None:
        result = UploadUrlResult(
            url="https://s3.amazonaws.com/...",
            method="PUT",
            headers={"Content-Type": "image/png"},
            expires_at=datetime.now(tz=timezone.utc),
            key="images/photo.png",
        )
        with pytest.raises(AttributeError):
            result.method = "POST"  # type: ignore[misc]
