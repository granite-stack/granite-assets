"""granite-assets – portable asset repository abstraction.

Public API
----------
Import the pieces you need directly from ``granite_assets``:

    from granite_assets import (
        IAssetRepository,
        AssetVisibility,
        AssetSaveRequest,
        AssetSaveResult,
        AssetDescriptor,
        AssetAccessUrl,
        UploadUrlResult,
        LocalNginxAssetRepositoryConfig,
        S3AssetRepositoryConfig,
        LocalNginxAssetRepository,
        build_asset_repository,
        AssetError,
        AssetNotFoundError,
        AssetAccessNotSupportedError,
        AssetConfigurationError,
    )

``S3AssetRepository`` is only available if the ``s3`` extra is installed.
"""

from granite_assets.contracts import IAssetRepository
from granite_assets.enums import AssetVisibility
from granite_assets.exceptions import (
    AssetAccessNotSupportedError,
    AssetConfigurationError,
    AssetError,
    AssetNotFoundError,
)
from granite_assets.factory import build_asset_repository
from granite_assets.models import (
    AssetAccessUrl,
    AssetDescriptor,
    AssetSaveRequest,
    AssetSaveResult,
    LocalNginxAssetRepositoryConfig,
    S3AssetRepositoryConfig,
    UploadUrlResult,
)
from granite_assets.repositories.local_nginx import LocalNginxAssetRepository

__all__ = [
    # Contract
    "IAssetRepository",
    # Enums
    "AssetVisibility",
    # Models
    "AssetSaveRequest",
    "AssetSaveResult",
    "AssetDescriptor",
    "AssetAccessUrl",
    "UploadUrlResult",
    # Configs
    "LocalNginxAssetRepositoryConfig",
    "S3AssetRepositoryConfig",
    # Repositories
    "LocalNginxAssetRepository",
    # Factory
    "build_asset_repository",
    # Exceptions
    "AssetError",
    "AssetNotFoundError",
    "AssetAccessNotSupportedError",
    "AssetConfigurationError",
]

try:
    from granite_assets.repositories.s3 import S3AssetRepository  # noqa: F401

    __all__ += ["S3AssetRepository"]
except ImportError:
    pass

__version__ = "0.1.0"
