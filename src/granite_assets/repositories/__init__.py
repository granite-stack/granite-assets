"""Repository implementations for granite-assets."""

from granite_assets.repositories.local_nginx import LocalNginxAssetRepository

__all__ = ["LocalNginxAssetRepository"]

try:
    from granite_assets.repositories.s3 import S3AssetRepository  # noqa: F401

    __all__ += ["S3AssetRepository"]
except ImportError:
    pass
