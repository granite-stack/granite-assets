"""Lightweight factory for building asset repositories from config objects.

The factory pattern is intentionally minimal: it avoids string-based dispatch
and keeps full type safety — the return type is narrowed to the concrete class
so IDEs and type checkers benefit without needing ``cast``.
"""

from __future__ import annotations

from typing import Union, overload

from granite_assets.models import (
    LocalNginxAssetRepositoryConfig,
    S3AssetRepositoryConfig,
)
from granite_assets.repositories.local_nginx import LocalNginxAssetRepository

AnyConfig = Union[LocalNginxAssetRepositoryConfig, S3AssetRepositoryConfig]


@overload
def build_asset_repository(
    config: LocalNginxAssetRepositoryConfig,
) -> LocalNginxAssetRepository: ...


@overload
def build_asset_repository(config: S3AssetRepositoryConfig) -> "S3AssetRepository": ...  # type: ignore[misc]  # noqa: F821


def build_asset_repository(config: AnyConfig) -> object:
    """Instantiate the correct repository for the given configuration object.

    The returned type is narrowed by overloads — type checkers will infer the
    concrete class when the config type is known at the call site.

    Args:
        config: A configuration dataclass.  Pass a
                :class:`LocalNginxAssetRepositoryConfig` for the local backend
                or a :class:`S3AssetRepositoryConfig` for AWS S3.

    Returns:
        A ready-to-use repository instance.

    Raises:
        TypeError: If the config type is not recognised.
        ImportError: If the S3 extra is not installed and an S3 config is given.

    Example::

        repo = build_asset_repository(
            S3AssetRepositoryConfig(bucket="my-bucket", region="eu-west-1")
        )
    """
    if isinstance(config, LocalNginxAssetRepositoryConfig):
        return LocalNginxAssetRepository(config)

    if isinstance(config, S3AssetRepositoryConfig):
        from granite_assets.repositories.s3 import S3AssetRepository  # noqa: PLC0415
        return S3AssetRepository(config)

    raise TypeError(
        f"Unsupported configuration type: {type(config).__name__!r}. "
        "Expected LocalNginxAssetRepositoryConfig or S3AssetRepositoryConfig."
    )
