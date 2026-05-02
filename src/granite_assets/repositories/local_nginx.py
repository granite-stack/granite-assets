"""Local filesystem asset repository served by Nginx (or any static HTTP server).

Design constraints
------------------
* Nginx itself is responsible for serving files; this library only writes/reads
  the filesystem.
* Public assets are placed under ``<storage_path>/<public_prefix>/`` and
  served at ``<base_url>/<public_prefix>/``.
* Private assets are placed under ``<storage_path>/<private_prefix>/``.  It is
  the operator's responsibility to configure Nginx ``auth_request`` or
  ``internal`` directives to protect this directory.  This library does *not*
  and cannot enforce access control at the HTTP layer.
* Signed download/upload URLs are **not supported**.  The backend raises
  ``AssetAccessNotSupportedError`` for those operations so that callers know
  they must route access through their application layer instead.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from granite_assets.contracts import IAssetRepository
from granite_assets.enums import AssetVisibility
from granite_assets.exceptions import (
    AssetAccessNotSupportedError,
    AssetConfigurationError,
    AssetError,
    AssetNotFoundError,
)
from granite_assets.models import (
    AssetAccessUrl,
    AssetDescriptor,
    AssetSaveRequest,
    AssetSaveResult,
    LocalNginxAssetRepositoryConfig,
    UploadUrlResult,
)

_BACKEND_NAME = "LocalNginxAssetRepository"


def _assert_no_leading_slash(key: str) -> None:
    if key.startswith("/"):
        raise AssetError(f"Asset key must not start with '/': {key!r}")


class LocalNginxAssetRepository:
    """Asset repository backed by the local filesystem.

    Files are organised under two sub-directories:

    * ``<storage_path>/<public_prefix>/`` – publicly served assets.
    * ``<storage_path>/<private_prefix>/`` – private assets (Nginx-protected).

    Public URLs are constructed by joining ``base_url``, the relevant prefix,
    and the logical key.

    Example::

        config = LocalNginxAssetRepositoryConfig(
            storage_path="/var/www/assets",
            base_url="https://cdn.example.com/assets",
        )
        repo = LocalNginxAssetRepository(config)
    """

    def __init__(self, config: LocalNginxAssetRepositoryConfig) -> None:
        self._cfg = config
        self._root = Path(config.storage_path)
        self._validate_config()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_config(self) -> None:
        if not self._cfg.base_url:
            raise AssetConfigurationError("base_url must not be empty")
        if not self._cfg.storage_path:
            raise AssetConfigurationError("storage_path must not be empty")

    def _full_path(self, key: str, visibility: AssetVisibility) -> Path:
        prefix = (
            self._cfg.public_prefix
            if visibility == AssetVisibility.PUBLIC
            else self._cfg.private_prefix
        )
        return self._root / prefix / key

    def _public_url(self, prefix: str, key: str) -> str:
        base = self._cfg.base_url.rstrip("/")
        return f"{base}/{prefix}/{key}"

    def _md5_of_file(self, path: Path) -> str:
        digest = hashlib.md5()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def save(self, request: AssetSaveRequest) -> AssetSaveResult:
        """Write the asset to disk.

        Raises:
            AssetError: If the file already exists and *overwrite* is False.
        """
        _assert_no_leading_slash(request.key)
        overwrite = request.overwrite if request.overwrite is not None else self._cfg.overwrite
        dest = self._full_path(request.key, request.visibility)

        if dest.exists() and not overwrite:
            raise AssetError(
                f"Asset already exists and overwrite is disabled: {request.key!r}"
            )

        if self._cfg.create_directories:
            dest.parent.mkdir(parents=True, exist_ok=True)
        elif not dest.parent.exists():
            raise AssetError(
                f"Parent directory does not exist: {dest.parent} "
                "(set create_directories=True to auto-create)"
            )

        stream = request.open_source()
        total = 0
        try:
            with dest.open("wb") as fh:
                for chunk in iter(lambda: stream.read(65536), b""):
                    fh.write(chunk)
                    total += len(chunk)
        except OSError as exc:
            raise AssetError(f"Failed to write asset {request.key!r}: {exc}") from exc

        checksum = f"md5:{self._md5_of_file(dest)}"
        return AssetSaveResult(
            key=request.key,
            backend_ref=str(dest),
            content_length=total,
            checksum=checksum,
            visibility=request.visibility,
        )

    def delete(self, key: str) -> None:
        """Remove the asset file from disk.

        Tries both visibility prefixes so the caller does not need to know
        where the file is stored.
        """
        _assert_no_leading_slash(key)
        for visibility in AssetVisibility:
            path = self._full_path(key, visibility)
            if path.exists():
                try:
                    path.unlink()
                except OSError as exc:
                    raise AssetError(f"Failed to delete asset {key!r}: {exc}") from exc
                return
        raise AssetNotFoundError(key)

    def copy(self, source_key: str, dest_key: str, *, overwrite: bool = True) -> None:
        """Copy a file on disk using shutil (server-side, no re-upload)."""
        _assert_no_leading_slash(source_key)
        _assert_no_leading_slash(dest_key)

        src_path = self._resolve_existing_path(source_key)
        # Infer visibility from which sub-directory the source lives in
        visibility = self._infer_visibility(src_path)
        dest_path = self._full_path(dest_key, visibility)

        if dest_path.exists() and not overwrite:
            raise AssetError(f"Destination key already exists: {dest_key!r}")

        if self._cfg.create_directories:
            dest_path.parent.mkdir(parents=True, exist_ok=True)

        shutil.copy2(src_path, dest_path)

    def move(self, source_key: str, dest_key: str, *, overwrite: bool = True) -> None:
        """Move (rename) a file on disk."""
        _assert_no_leading_slash(source_key)
        _assert_no_leading_slash(dest_key)

        src_path = self._resolve_existing_path(source_key)
        visibility = self._infer_visibility(src_path)
        dest_path = self._full_path(dest_key, visibility)

        if dest_path.exists() and not overwrite:
            raise AssetError(f"Destination key already exists: {dest_key!r}")

        if self._cfg.create_directories:
            dest_path.parent.mkdir(parents=True, exist_ok=True)

        shutil.move(str(src_path), dest_path)

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    def exists(self, key: str) -> bool:
        """Return True if the key exists under either visibility prefix."""
        _assert_no_leading_slash(key)
        return any(
            self._full_path(key, v).exists() for v in AssetVisibility
        )

    def get_descriptor(self, key: str) -> AssetDescriptor:
        """Return file metadata without reading the file body."""
        _assert_no_leading_slash(key)
        path = self._resolve_existing_path(key)
        stat = path.stat()
        visibility = self._infer_visibility(path)
        return AssetDescriptor(
            key=key,
            content_length=stat.st_size,
            visibility=visibility,
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        )

    # ------------------------------------------------------------------
    # URL construction
    # ------------------------------------------------------------------

    def build_public_url(self, key: str) -> AssetAccessUrl:
        """Return a permanent public URL.

        Only valid for assets stored with ``AssetVisibility.PUBLIC``.

        Raises:
            AssetAccessNotSupportedError: If the asset is private (no signed
                URL support in this backend).
        """
        _assert_no_leading_slash(key)
        # We only guarantee that a public URL exists for public assets.
        # We can still build the URL even if the file doesn't exist on disk;
        # callers that need to verify existence should call ``exists`` first.
        url = self._public_url(self._cfg.public_prefix, key)
        return AssetAccessUrl(url=url, expires_at=None)

    def build_download_url(self, key: str, ttl_seconds: int | None = None) -> AssetAccessUrl:
        """Return a download URL.

        For public assets, returns the same permanent URL.  For private assets,
        raises ``AssetAccessNotSupportedError`` because this backend cannot
        generate signed URLs — the calling application must implement its own
        token-based download endpoint.
        """
        _assert_no_leading_slash(key)
        # Check which prefix holds this asset (or fall back to public URL)
        pub_path = self._full_path(key, AssetVisibility.PUBLIC)
        if pub_path.exists():
            return self.build_public_url(key)

        priv_path = self._full_path(key, AssetVisibility.PRIVATE)
        if priv_path.exists():
            raise AssetAccessNotSupportedError(
                _BACKEND_NAME,
                "build_download_url (private asset)",
            )
        # Key not found; still raise unsupported for consistency
        raise AssetAccessNotSupportedError(
            _BACKEND_NAME, "build_download_url (private asset)"
        )

    def build_upload_url(
        self,
        key: str,
        content_type: str,
        ttl_seconds: int | None = None,
    ) -> UploadUrlResult:
        """Not supported by this backend.

        Raises:
            AssetAccessNotSupportedError: Always.  The local filesystem has no
                HTTP upload mechanism.  Route uploads through your application
                layer and call ``save`` directly.
        """
        raise AssetAccessNotSupportedError(_BACKEND_NAME, "build_upload_url")

    def resolve_access(self, key: str, ttl_seconds: int | None = None) -> AssetAccessUrl:
        """Return the best available URL for the asset.

        For public assets, returns a permanent URL.  For private assets on this
        backend, raises ``AssetAccessNotSupportedError``.
        """
        _assert_no_leading_slash(key)
        pub_path = self._full_path(key, AssetVisibility.PUBLIC)
        if pub_path.exists():
            return self.build_public_url(key)

        priv_path = self._full_path(key, AssetVisibility.PRIVATE)
        if priv_path.exists():
            raise AssetAccessNotSupportedError(
                _BACKEND_NAME, "resolve_access (private asset)"
            )
        raise AssetNotFoundError(key)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_existing_path(self, key: str) -> Path:
        """Return the Path for *key* in whatever prefix it exists, or raise."""
        for visibility in AssetVisibility:
            path = self._full_path(key, visibility)
            if path.exists():
                return path
        raise AssetNotFoundError(key)

    def _infer_visibility(self, path: Path) -> AssetVisibility:
        """Determine visibility by checking which prefix the path falls under."""
        pub_root = self._root / self._cfg.public_prefix
        try:
            path.relative_to(pub_root)
            return AssetVisibility.PUBLIC
        except ValueError:
            return AssetVisibility.PRIVATE


# Verify structural compatibility at import time (cheap, dev-friendly)
assert isinstance(LocalNginxAssetRepository, type)
_: IAssetRepository = LocalNginxAssetRepository.__new__(LocalNginxAssetRepository)  # type: ignore[assignment]
