"""Tests for LocalNginxAssetRepository."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from granite_assets.enums import AssetVisibility
from granite_assets.exceptions import (
    AssetAccessNotSupportedError,
    AssetConfigurationError,
    AssetNotFoundError,
)
from granite_assets.models import AssetSaveRequest, LocalNginxAssetRepositoryConfig
from granite_assets.repositories.local_nginx import LocalNginxAssetRepository


@pytest.fixture()
def storage_path(tmp_path: Path) -> Path:
    return tmp_path / "assets"


@pytest.fixture()
def config(storage_path: Path) -> LocalNginxAssetRepositoryConfig:
    return LocalNginxAssetRepositoryConfig(
        storage_path=str(storage_path),
        base_url="https://static.example.com/assets",
        create_directories=True,
    )


@pytest.fixture()
def repo(config: LocalNginxAssetRepositoryConfig) -> LocalNginxAssetRepository:
    return LocalNginxAssetRepository(config)


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------


def test_missing_base_url_raises() -> None:
    cfg = LocalNginxAssetRepositoryConfig(storage_path="/tmp", base_url="")
    with pytest.raises(AssetConfigurationError, match="base_url"):
        LocalNginxAssetRepository(cfg)


def test_missing_storage_path_raises() -> None:
    cfg = LocalNginxAssetRepositoryConfig(storage_path="", base_url="http://x.com")
    with pytest.raises(AssetConfigurationError, match="storage_path"):
        LocalNginxAssetRepository(cfg)


# ---------------------------------------------------------------------------
# save / exists
# ---------------------------------------------------------------------------


def test_save_public_asset(repo: LocalNginxAssetRepository, storage_path: Path) -> None:
    req = AssetSaveRequest(
        key="images/logo.png",
        source=b"\x89PNG\r\n",
        content_type="image/png",
        visibility=AssetVisibility.PUBLIC,
    )
    result = repo.save(req)

    assert result.key == "images/logo.png"
    assert result.visibility == AssetVisibility.PUBLIC
    assert result.content_length == 6
    assert result.checksum is not None
    assert (storage_path / "public" / "images" / "logo.png").exists()


def test_save_private_asset(
    repo: LocalNginxAssetRepository, storage_path: Path
) -> None:
    req = AssetSaveRequest(
        key="reports/q1.pdf",
        source=b"%PDF-1.4",
        content_type="application/pdf",
        visibility=AssetVisibility.PRIVATE,
    )
    result = repo.save(req)

    assert result.visibility == AssetVisibility.PRIVATE
    assert (storage_path / "private" / "reports" / "q1.pdf").exists()


def test_save_from_stream(repo: LocalNginxAssetRepository) -> None:
    stream = io.BytesIO(b"stream content")
    req = AssetSaveRequest(
        key="docs/readme.txt",
        source=stream,
        content_type="text/plain",
        visibility=AssetVisibility.PUBLIC,
    )
    result = repo.save(req)
    assert result.content_length == 14


def test_exists_true_after_save(repo: LocalNginxAssetRepository) -> None:
    req = AssetSaveRequest(key="a.txt", source=b"x", content_type="text/plain")
    repo.save(req)
    assert repo.exists("a.txt") is True


def test_exists_false_for_unknown(repo: LocalNginxAssetRepository) -> None:
    assert repo.exists("does/not/exist.txt") is False


def test_leading_slash_raises(repo: LocalNginxAssetRepository) -> None:
    from granite_assets.exceptions import AssetError

    req = AssetSaveRequest(key="/bad/key.txt", source=b"x", content_type="text/plain")
    with pytest.raises(AssetError, match="must not start"):
        repo.save(req)


# ---------------------------------------------------------------------------
# Auto-generated key: <uuid>/<uuid>.<ext>
# ---------------------------------------------------------------------------


def test_save_without_key_generates_uuid_folder_structure(
    repo: LocalNginxAssetRepository, storage_path: Path
) -> None:
    """When key is omitted, save() must store the file at <uuid>/<uuid>.<ext>."""
    result = repo.save(
        AssetSaveRequest(source=b"img", content_type="image/png", filename="photo.png")
    )

    parts = result.key.split("/")
    assert len(parts) == 2, f"Expected <uuid>/<uuid>.ext, got {result.key!r}"
    folder, filename_with_ext = parts
    stem, ext = filename_with_ext.rsplit(".", 1)
    assert folder == stem, "Folder UUID must match filename UUID"
    assert ext == "png"


def test_save_without_key_file_exists_on_disk(
    repo: LocalNginxAssetRepository, storage_path: Path
) -> None:
    """The auto-generated key must correspond to a real file on disk."""
    result = repo.save(
        AssetSaveRequest(
            source=b"hello", content_type="text/plain", filename="note.txt"
        )
    )

    assert repo.exists(result.key)


def test_save_without_key_no_extension(
    repo: LocalNginxAssetRepository,
) -> None:
    """A file with no extension produces <uuid>/<uuid> (no trailing dot)."""
    result = repo.save(
        AssetSaveRequest(source=b"raw", content_type="application/octet-stream")
    )

    parts = result.key.split("/")
    assert len(parts) == 2
    assert parts[0] == parts[1], (
        "Folder and filename UUIDs must match when no extension"
    )


def test_save_with_explicit_key_uses_it_unchanged(
    repo: LocalNginxAssetRepository,
) -> None:
    """When key is provided explicitly it must be used as-is (backward compat)."""
    result = repo.save(
        AssetSaveRequest(
            source=b"data", content_type="text/plain", key="docs/readme.txt"
        )
    )

    assert result.key == "docs/readme.txt"


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_existing_asset(repo: LocalNginxAssetRepository) -> None:
    repo.save(
        AssetSaveRequest(
            key="to-delete.txt",
            source=b"bye",
            content_type="text/plain",
            visibility=AssetVisibility.PUBLIC,
        )
    )
    repo.delete("to-delete.txt")
    assert repo.exists("to-delete.txt") is False


def test_delete_nonexistent_raises(repo: LocalNginxAssetRepository) -> None:
    with pytest.raises(AssetNotFoundError):
        repo.delete("ghost.txt")


# ---------------------------------------------------------------------------
# copy / move
# ---------------------------------------------------------------------------


def test_copy_asset(repo: LocalNginxAssetRepository, storage_path: Path) -> None:
    repo.save(
        AssetSaveRequest(
            key="original.txt",
            source=b"hello",
            content_type="text/plain",
            visibility=AssetVisibility.PUBLIC,
        )
    )
    repo.copy("original.txt", "copy.txt")
    assert (storage_path / "public" / "copy.txt").exists()
    assert (storage_path / "public" / "original.txt").exists()


def test_move_asset(repo: LocalNginxAssetRepository, storage_path: Path) -> None:
    repo.save(
        AssetSaveRequest(
            key="source.txt",
            source=b"data",
            content_type="text/plain",
            visibility=AssetVisibility.PUBLIC,
        )
    )
    repo.move("source.txt", "destination.txt")
    assert (storage_path / "public" / "destination.txt").exists()
    assert not (storage_path / "public" / "source.txt").exists()


# ---------------------------------------------------------------------------
# get_descriptor
# ---------------------------------------------------------------------------


def test_get_descriptor(repo: LocalNginxAssetRepository) -> None:
    repo.save(
        AssetSaveRequest(
            key="meta.txt",
            source=b"metadata test",
            content_type="text/plain",
            visibility=AssetVisibility.PUBLIC,
        )
    )
    desc = repo.get_descriptor("meta.txt")
    assert desc.key == "meta.txt"
    assert desc.content_length == 13
    assert desc.visibility == AssetVisibility.PUBLIC
    assert desc.last_modified is not None


def test_get_descriptor_missing_raises(repo: LocalNginxAssetRepository) -> None:
    with pytest.raises(AssetNotFoundError):
        repo.get_descriptor("missing.txt")


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


def test_build_public_url(repo: LocalNginxAssetRepository) -> None:
    url = repo.build_public_url("images/hero.jpg")
    assert url.url == "https://static.example.com/assets/public/images/hero.jpg"
    assert url.is_permanent is True


def test_build_download_url_public_asset(repo: LocalNginxAssetRepository) -> None:
    repo.save(
        AssetSaveRequest(
            key="public-doc.txt",
            source=b"x",
            content_type="text/plain",
            visibility=AssetVisibility.PUBLIC,
        )
    )
    url = repo.build_download_url("public-doc.txt")
    assert "public-doc.txt" in url.url
    assert url.is_permanent is True


def test_build_download_url_private_raises(repo: LocalNginxAssetRepository) -> None:
    repo.save(
        AssetSaveRequest(
            key="secret.txt",
            source=b"secret",
            content_type="text/plain",
            visibility=AssetVisibility.PRIVATE,
        )
    )
    with pytest.raises(AssetAccessNotSupportedError):
        repo.build_download_url("secret.txt")


def test_build_upload_url_raises(repo: LocalNginxAssetRepository) -> None:
    with pytest.raises(AssetAccessNotSupportedError):
        repo.build_upload_url("anything.txt", "text/plain")


# ---------------------------------------------------------------------------
# build_upload_url — tus / tusd
# ---------------------------------------------------------------------------


@pytest.fixture()
def tusd_config(storage_path: Path) -> LocalNginxAssetRepositoryConfig:
    return LocalNginxAssetRepositoryConfig(
        storage_path=str(storage_path),
        base_url="https://static.example.com/assets",
        create_directories=True,
        tusd_url="http://localhost:1080",
        upload_secret="sup3rs3cr3t",
        upload_ttl_seconds=1800,
    )


@pytest.fixture()
def tusd_repo(
    tusd_config: LocalNginxAssetRepositoryConfig,
) -> LocalNginxAssetRepository:
    return LocalNginxAssetRepository(tusd_config)


def test_build_upload_url_no_tusd_url_raises(storage_path: Path) -> None:
    cfg = LocalNginxAssetRepositoryConfig(
        storage_path=str(storage_path),
        base_url="http://example.com",
        upload_secret="s3cr3t",
    )
    repo = LocalNginxAssetRepository(cfg)
    with pytest.raises(AssetAccessNotSupportedError, match="tusd_url"):
        repo.build_upload_url("file.pdf", "application/pdf")


def test_build_upload_url_no_upload_secret_raises(storage_path: Path) -> None:
    cfg = LocalNginxAssetRepositoryConfig(
        storage_path=str(storage_path),
        base_url="http://example.com",
        tusd_url="http://localhost:1080",
    )
    repo = LocalNginxAssetRepository(cfg)
    with pytest.raises(AssetAccessNotSupportedError, match="upload_secret"):
        repo.build_upload_url("file.pdf", "application/pdf")


def test_build_upload_url_returns_tus_result(
    tusd_repo: LocalNginxAssetRepository,
) -> None:
    from granite_assets.models import UploadUrlResult

    result = tusd_repo.build_upload_url("docs/report.pdf", "application/pdf")

    assert isinstance(result, UploadUrlResult)
    assert result.method == "POST"
    assert result.url == "http://localhost:1080/files/"
    assert result.key == "docs/report.pdf"
    assert result.expires_at is not None


def test_build_upload_url_tus_headers_present(
    tusd_repo: LocalNginxAssetRepository,
) -> None:
    result = tusd_repo.build_upload_url("img/photo.jpg", "image/jpeg")

    assert result.headers["Tus-Resumable"] == "1.0.0"
    assert "Upload-Metadata" in result.headers
    assert "Content-Length" in result.headers


def test_build_upload_url_metadata_contains_expected_keys(
    tusd_repo: LocalNginxAssetRepository,
) -> None:
    import base64

    result = tusd_repo.build_upload_url("docs/file.txt", "text/plain")
    metadata = result.headers["Upload-Metadata"]

    # Parse "key base64value, ..." into a dict
    parsed: dict[str, str] = {}
    for entry in metadata.split(","):
        parts = entry.strip().split(" ", 1)
        assert len(parts) == 2, f"Malformed metadata entry: {entry!r}"
        parsed[parts[0]] = base64.b64decode(parts[1]).decode()

    assert parsed["asset-key"] == "docs/file.txt"
    assert parsed["content-type"] == "text/plain"
    assert parsed["visibility"] == "private"
    assert parsed["upload-expires"].isdigit()
    assert len(parsed["upload-token"]) == 64  # SHA-256 hex


def test_build_upload_url_token_is_valid_hmac(
    tusd_repo: LocalNginxAssetRepository,
) -> None:
    import base64
    import hmac as _hmac

    key = "data/archive.zip"
    content_type = "application/zip"
    result = tusd_repo.build_upload_url(key, content_type)

    metadata = result.headers["Upload-Metadata"]
    parsed = {
        p.split(" ")[0]: base64.b64decode(p.split(" ")[1]).decode()
        for p in (e.strip() for e in metadata.split(","))
    }

    expires = int(parsed["upload-expires"])
    token = parsed["upload-token"]
    visibility = parsed["visibility"]
    payload = f"{expires}:{key}:{visibility}:{content_type}"
    expected = _hmac.new(b"sup3rs3cr3t", payload.encode(), "sha256").hexdigest()

    assert token == expected


def test_build_upload_url_ttl_override(tusd_repo: LocalNginxAssetRepository) -> None:
    import base64
    import time

    before = int(time.time())
    result = tusd_repo.build_upload_url(
        "a.bin", "application/octet-stream", ttl_seconds=7200
    )
    after = int(time.time())

    metadata = result.headers["Upload-Metadata"]
    parsed = {
        p.split(" ")[0]: base64.b64decode(p.split(" ")[1]).decode()
        for p in (e.strip() for e in metadata.split(","))
    }
    expires = int(parsed["upload-expires"])

    assert before + 7200 <= expires <= after + 7200


def test_build_upload_url_trailing_slash_normalized(storage_path: Path) -> None:
    cfg = LocalNginxAssetRepositoryConfig(
        storage_path=str(storage_path),
        base_url="http://example.com",
        tusd_url="http://localhost:1080/",  # trailing slash
        upload_secret="s3c",
    )
    repo = LocalNginxAssetRepository(cfg)
    result = repo.build_upload_url("f.txt", "text/plain")
    assert result.url == "http://localhost:1080/files/"
    assert not result.url.startswith("http://localhost:1080//")


def test_build_upload_url_visibility_public(
    tusd_repo: LocalNginxAssetRepository,
) -> None:
    import base64

    result = tusd_repo.build_upload_url(
        "images/logo.png", "image/png", visibility=AssetVisibility.PUBLIC
    )
    metadata = result.headers["Upload-Metadata"]
    parsed = {
        p.split(" ")[0]: base64.b64decode(p.split(" ")[1]).decode()
        for p in (e.strip() for e in metadata.split(","))
    }
    assert parsed["visibility"] == "public"


def test_build_upload_url_key_with_leading_slash_raises(
    tusd_repo: LocalNginxAssetRepository,
) -> None:
    from granite_assets.exceptions import AssetError

    with pytest.raises(AssetError, match="must not start with"):
        tusd_repo.build_upload_url("/bad/key.txt", "text/plain")


# ---------------------------------------------------------------------------
# resolve_access
# ---------------------------------------------------------------------------


def test_resolve_access_public(repo: LocalNginxAssetRepository) -> None:
    repo.save(
        AssetSaveRequest(
            key="open.txt",
            source=b"open",
            content_type="text/plain",
            visibility=AssetVisibility.PUBLIC,
        )
    )
    access = repo.resolve_access("open.txt")
    assert access.is_permanent is True


def test_resolve_access_missing_raises(repo: LocalNginxAssetRepository) -> None:
    with pytest.raises(AssetNotFoundError):
        repo.resolve_access("not-here.txt")
