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


def test_save_private_asset(repo: LocalNginxAssetRepository, storage_path: Path) -> None:
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
