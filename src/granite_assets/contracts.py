"""Repository contract (Protocol) for granite-assets.

Defines the ``IAssetRepository`` structural interface.  Any class that
implements these methods is considered a valid repository without needing to
inherit from this Protocol explicitly (structural subtyping / duck typing).

Design notes
------------
* ``save`` receives a stream-based request so that large files are never fully
  buffered in application memory.
* URL construction methods are pure and *do not* hit the network; they only
  derive a URL string from the key and configuration.
* ``get_descriptor`` is a lightweight ``HEAD``-like call that retrieves metadata
  without downloading the asset body.
* ``copy`` and ``move`` are provided because they map directly to cheap
  server-side operations in most backends (S3 server-side copy, local
  ``shutil.copy2`` + ``unlink``).  Omitting them would force callers to
  download + re-upload unnecessarily.
* All methods are **synchronous**.  The library is intended to run inside
  existing sync or async applications where sync I/O inside a thread pool is
  the normal pattern for blocking operations (FastAPI ``run_in_executor``,
  Celery tasks, Django views, etc.).  Async variants can be added in a future
  minor version without breaking the current API.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from granite_assets.models import (
    AssetAccessUrl,
    AssetDescriptor,
    AssetSaveRequest,
    AssetSaveResult,
    UploadUrlResult,
)


@runtime_checkable
class IAssetRepository(Protocol):
    """Structural interface for asset repositories.

    Implementations must provide all methods below.  The ``@runtime_checkable``
    decorator allows ``isinstance(repo, IAssetRepository)`` checks at runtime,
    which is useful in dependency-injection containers and factory helpers.
    """

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def save(self, request: AssetSaveRequest) -> AssetSaveResult:
        """Persist an asset to the backend.

        Args:
            request: Fully described save request including stream, key,
                     content type and visibility.

        Returns:
            Metadata about the stored asset.

        Raises:
            AssetError: If writing fails (I/O error, permission, etc.).
        """
        ...

    def delete(self, key: str) -> None:
        """Remove an asset from the backend.

        Args:
            key: Logical key of the asset to remove.

        Raises:
            AssetNotFoundError: If the key does not exist.
        """
        ...

    def copy(self, source_key: str, dest_key: str, *, overwrite: bool = True) -> None:
        """Copy an asset to a new key without downloading it.

        Args:
            source_key: Key of the asset to copy.
            dest_key:   Key of the destination.
            overwrite:  Whether to overwrite an existing destination key.

        Raises:
            AssetNotFoundError: If *source_key* does not exist.
        """
        ...

    def move(self, source_key: str, dest_key: str, *, overwrite: bool = True) -> None:
        """Move (rename) an asset.

        Equivalent to ``copy`` followed by ``delete`` on the source.

        Args:
            source_key: Key of the asset to move.
            dest_key:   Key of the destination.
            overwrite:  Whether to overwrite an existing destination key.

        Raises:
            AssetNotFoundError: If *source_key* does not exist.
        """
        ...

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    def exists(self, key: str) -> bool:
        """Return ``True`` if the asset exists in the backend.

        Args:
            key: Logical key to check.
        """
        ...

    def get_descriptor(self, key: str) -> AssetDescriptor:
        """Retrieve metadata for an existing asset without downloading it.

        Args:
            key: Logical key of the asset.

        Returns:
            Metadata descriptor.

        Raises:
            AssetNotFoundError: If the key does not exist.
        """
        ...

    # ------------------------------------------------------------------
    # URL construction
    # ------------------------------------------------------------------

    def build_public_url(self, key: str) -> AssetAccessUrl:
        """Return a permanent, publicly accessible URL for the asset.

        Only valid for assets stored with ``AssetVisibility.PUBLIC``.

        Args:
            key: Logical key of the asset.

        Returns:
            A permanent :class:`AssetAccessUrl` (``expires_at`` is ``None``).

        Raises:
            AssetAccessNotSupportedError: If the backend or asset visibility does
                not support public URLs.
        """
        ...

    def build_download_url(self, key: str, ttl_seconds: int | None = None) -> AssetAccessUrl:
        """Return a time-limited URL suitable for downloading the asset.

        For public assets this may return the same permanent URL.  For private
        assets it returns a signed URL that expires after *ttl_seconds*.

        Args:
            key:         Logical key of the asset.
            ttl_seconds: Override the default TTL from configuration.

        Returns:
            :class:`AssetAccessUrl` with an ``expires_at`` for private assets.

        Raises:
            AssetAccessNotSupportedError: If signed download URLs are not
                supported by this backend.
        """
        ...

    def build_upload_url(
        self,
        key: str,
        content_type: str,
        ttl_seconds: int | None = None,
    ) -> UploadUrlResult:
        """Return a pre-signed URL that allows a client to upload an asset.

        The client should send the file as the body of an HTTP PUT request to
        the returned URL, including the headers specified in
        :attr:`UploadUrlResult.headers`.

        Args:
            key:          Logical key under which the asset will be stored.
            content_type: MIME type that the client must declare in the upload.
            ttl_seconds:  Override the default TTL from configuration.

        Returns:
            :class:`UploadUrlResult` with the upload URL and required headers.

        Raises:
            AssetAccessNotSupportedError: If pre-signed upload URLs are not
                supported by this backend (e.g. local filesystem).
        """
        ...

    def resolve_access(self, key: str, ttl_seconds: int | None = None) -> AssetAccessUrl:
        """Convenience helper that returns the best available URL for the asset.

        For public assets returns the permanent public URL; for private assets
        returns a signed download URL.

        Args:
            key:         Logical key of the asset.
            ttl_seconds: TTL hint for signed URLs (ignored for public assets).

        Returns:
            The most appropriate :class:`AssetAccessUrl` for this asset.
        """
        ...
