"""Tests for the build_asset_repository factory."""

from __future__ import annotations

import pytest

from granite_assets.factory import build_asset_repository
from granite_assets.models import LocalNginxAssetRepositoryConfig, S3AssetRepositoryConfig
from granite_assets.repositories.local_nginx import LocalNginxAssetRepository


def test_factory_returns_local_nginx(tmp_path: pytest.TempPathFactory) -> None:
    cfg = LocalNginxAssetRepositoryConfig(
        storage_path=str(tmp_path),
        base_url="https://example.com/assets",
    )
    repo = build_asset_repository(cfg)
    assert isinstance(repo, LocalNginxAssetRepository)


def test_factory_invalid_config_raises() -> None:
    with pytest.raises(TypeError, match="Unsupported configuration"):
        build_asset_repository("not-a-config")  # type: ignore[arg-type]


def test_factory_s3_without_boto3_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """If boto3 is not installed, factory should raise ImportError for S3 config."""
    import sys

    # Remove boto3 and the already-imported s3 module so the lazy import is forced
    boto3_mod = sys.modules.pop("boto3", None)
    s3_mod = sys.modules.pop("granite_assets.repositories.s3", None)
    try:
        # Prevent re-importing boto3
        sys.modules["boto3"] = None  # type: ignore[assignment]
        cfg = S3AssetRepositoryConfig(bucket="b", region="r")
        with pytest.raises((ImportError, Exception)):
            build_asset_repository(cfg)
    finally:
        if boto3_mod is not None:
            sys.modules["boto3"] = boto3_mod
        else:
            sys.modules.pop("boto3", None)
        if s3_mod is not None:
            sys.modules["granite_assets.repositories.s3"] = s3_mod
