"""Exceptions raised by granite-assets repositories."""


class AssetError(Exception):
    """Base exception for all granite-assets errors."""


class AssetNotFoundError(AssetError):
    """Raised when an asset key does not exist in the repository."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"Asset not found: {key!r}")


class AssetAccessNotSupportedError(AssetError):
    """Raised when the requested URL type is not supported by this backend.

    For example, ``build_upload_url`` on a local-nginx repository will raise
    this because there is no built-in mechanism to generate a pre-signed upload
    endpoint from a plain filesystem.
    """

    def __init__(self, backend: str, operation: str) -> None:
        self.backend = backend
        self.operation = operation
        super().__init__(
            f"Operation {operation!r} is not supported by backend {backend!r}"
        )


class AssetConfigurationError(AssetError):
    """Raised when a repository is misconfigured (missing field, bad value, etc.)."""
