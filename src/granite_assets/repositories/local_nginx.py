"""Local filesystem asset repository served by Nginx (or any static HTTP server).

Design constraints
------------------
* Nginx itself is responsible for serving files; this library only writes/reads
  the filesystem.
* Public assets are placed under ``<storage_path>/<public_prefix>/`` and
  served at ``<base_url>/<public_prefix>/``.
* Private assets are placed under ``<storage_path>/<private_prefix>/``.
* **Signed URLs** for private assets are supported when
  ``LocalNginxAssetRepositoryConfig.secure_link_secret`` is set.  The token
  algorithm matches Nginx's ``ngx_http_secure_link_module`` with the
  ``secure_link_md5`` directive.  Nginx validates tokens server-side without
  any round-trip to the application.
* When ``secure_link_secret`` is *not* set, private asset URL methods raise
  ``AssetAccessNotSupportedError`` so that callers know they must route access
  through their own application layer.

Signed URL algorithm (compatible with ``ngx_http_secure_link_module``)
-----------------------------------------------------------------------
Given:

* ``expires``   — Unix timestamp (int) when the URL expires.
* ``uri``       — Full URI path component of the asset URL, e.g.
                  ``/assets/private/reports/q1.pdf``.
* ``secret``    — Shared secret string (``secure_link_secret``).

The token is computed as::

    raw   = f"{expires}{uri} {secret}".encode("utf-8")
    token = base64.urlsafe_b64encode(md5(raw).digest()).rstrip(b"=").decode()

The resulting URL is::

    {base_url}/{private_prefix}/{key}?md5={token}&expires={expires}

The Nginx directive that validates this token is::

    secure_link $arg_md5,$arg_expires;
    secure_link_md5 "$secure_link_expires$uri YOUR_SECRET_HERE";
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import shutil
import time
import urllib.parse
import uuid as _uuid_mod
from datetime import UTC, datetime
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


def _resolve_asset_key(key: str | None, filename: str | None) -> str:
    """Return the final storage key.

    Three cases:
    * *key* is ``None``  →  auto-generate ``<uuid>/<uuid>.<ext>``.
    * *key* has no file extension  →  treat it as a folder prefix and append
      ``/<last_segment><ext>`` so callers can pass ``visibility/uuid`` and get
      back ``visibility/uuid/uuid.ext``.
    * *key* has a file extension  →  use it unchanged (backward-compatible).
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if key is None:
        asset_id = str(_uuid_mod.uuid4())
        return f"{asset_id}/{asset_id}{ext}"
    _, key_ext = os.path.splitext(key)
    if not key_ext:
        last_segment = key.rstrip("/").rsplit("/", 1)[-1]
        return f"{key}/{last_segment}{ext}"
    return key


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

    def _uri_path(self, prefix: str, key: str) -> str:
        """Return the URI *path* component for the given prefix and key.

        This is the portion that Nginx receives as ``$uri`` and that must be
        embedded verbatim in the secure-link HMAC input.

        Example::

            # base_url = "http://localhost:8080/assets"
            # prefix   = "private"
            # key      = "reports/q1.pdf"
            # → "/assets/private/reports/q1.pdf"
        """
        parsed = urllib.parse.urlparse(self._cfg.base_url)
        base_path = parsed.path.rstrip("/")
        return f"{base_path}/{prefix}/{key}"

    def _build_secure_link_token(self, uri_path: str, expires: int) -> str:
        """Compute the Nginx ``secure_link_md5`` token for *uri_path*.

        The token is an MD5 digest of ``"{expires}{uri_path} {secret}"``
        encoded as URL-safe base64 **without** padding, which is exactly what
        Nginx's ``ngx_http_secure_link_module`` expects.

        Args:
            uri_path: Full URI path as Nginx will see it in ``$uri``, e.g.
                      ``"/assets/private/reports/q1.pdf"``.
            expires:  Unix timestamp (seconds since epoch) after which the URL
                      should be rejected by Nginx with 410 Gone.

        Returns:
            URL-safe base64 token string (no trailing ``=``).

        Raises:
            AssetAccessNotSupportedError: If ``secure_link_secret`` is not
                configured on this repository instance.
        """
        if not self._cfg.secure_link_secret:
            raise AssetAccessNotSupportedError(
                _BACKEND_NAME, "signed URLs (secure_link_secret not configured)"
            )
        raw = f"{expires}{uri_path} {self._cfg.secure_link_secret}".encode()
        digest = hashlib.md5(raw).digest()  # noqa: S324 — required by Nginx protocol
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    def _build_signed_private_url(
        self, key: str, ttl_seconds: int | None
    ) -> AssetAccessUrl:
        """Build a time-limited signed URL for a private asset.

        Args:
            key:         Logical key of the asset (no leading slash).
            ttl_seconds: Lifetime of the URL in seconds.  Defaults to
                         ``config.secure_link_ttl_seconds`` (3600 s = 1 h).

        Returns:
            :class:`AssetAccessUrl` with ``expires_at`` set to the expiry
            timestamp in UTC.

        Raises:
            AssetAccessNotSupportedError: If ``secure_link_secret`` is not set.
        """
        ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else self._cfg.secure_link_ttl_seconds
        )
        expires = int(time.time()) + ttl
        prefix = self._cfg.private_prefix
        uri_path = self._uri_path(prefix, key)
        token = self._build_secure_link_token(uri_path, expires)
        base = self._cfg.base_url.rstrip("/")
        url = f"{base}/{prefix}/{key}?md5={token}&expires={expires}"
        return AssetAccessUrl(
            url=url,
            expires_at=datetime.fromtimestamp(expires, tz=UTC),
        )

    def _build_upload_token(
        self,
        key: str,
        visibility: AssetVisibility,
        content_type: str,
        expires: int,
    ) -> str:
        """Compute an HMAC-SHA256 upload token for use in tusd pre-create hooks.

        The token signs ``"{expires}:{key}:{visibility}:{content_type}"`` with
        ``upload_secret`` (SHA-256).  The tusd hook must replicate this to
        verify incoming uploads.

        Returns:
            Lowercase hex-encoded HMAC-SHA256 digest.
        """
        payload = f"{expires}:{key}:{visibility.value}:{content_type}"
        return hmac.new(
            self._cfg.upload_secret.encode("utf-8"),  # type: ignore[union-attr]
            payload.encode("utf-8"),
            "sha256",
        ).hexdigest()

    def _tus_metadata_header(
        self,
        key: str,
        visibility: AssetVisibility,
        content_type: str,
        token: str,
        expires: int,
    ) -> str:
        """Build the tus ``Upload-Metadata`` header value.

        The tus protocol requires each value to be base64-encoded.  Entries
        are ``"<key> <base64-value>"`` separated by commas.  The tusd hook
        reads these fields to validate and route the upload.
        """

        def _b64(s: str) -> str:
            return base64.b64encode(s.encode("utf-8")).decode()

        return ", ".join(
            [
                f"asset-key {_b64(key)}",
                f"content-type {_b64(content_type)}",
                f"visibility {_b64(visibility.value)}",
                f"upload-expires {_b64(str(expires))}",
                f"upload-token {_b64(token)}",
            ]
        )

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
        key = _resolve_asset_key(request.key, request.filename)
        _assert_no_leading_slash(key)
        overwrite = (
            request.overwrite if request.overwrite is not None else self._cfg.overwrite
        )
        dest = self._full_path(key, request.visibility)

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
            raise AssetError(f"Failed to write asset {key!r}: {exc}") from exc

        checksum = f"md5:{self._md5_of_file(dest)}"
        return AssetSaveResult(
            key=key,
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
        return any(self._full_path(key, v).exists() for v in AssetVisibility)

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
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
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

    def build_download_url(
        self, key: str, ttl_seconds: int | None = None
    ) -> AssetAccessUrl:
        """Return a download URL for the asset.

        * **Public assets** → permanent URL (no token required).
        * **Private assets with ``secure_link_secret`` configured** → signed
          URL valid for *ttl_seconds* seconds (default:
          ``config.secure_link_ttl_seconds``).
        * **Private assets without ``secure_link_secret``** → raises
          :exc:`AssetAccessNotSupportedError`.  The caller must proxy downloads
          through the application layer.

        Args:
            key:         Logical key of the asset (no leading slash).
            ttl_seconds: Override the default signed-URL TTL for this call only.

        Returns:
            :class:`AssetAccessUrl` with ``expires_at=None`` for public assets
            or a UTC datetime for signed private URLs.

        Raises:
            AssetAccessNotSupportedError: For private assets when no secret is
                configured, or when the asset is not found.
        """
        _assert_no_leading_slash(key)
        pub_path = self._full_path(key, AssetVisibility.PUBLIC)
        if pub_path.exists():
            return self.build_public_url(key)

        priv_path = self._full_path(key, AssetVisibility.PRIVATE)
        if priv_path.exists():
            if self._cfg.secure_link_secret:
                return self._build_signed_private_url(key, ttl_seconds)
            raise AssetAccessNotSupportedError(
                _BACKEND_NAME,
                "build_download_url (private asset"
                " — set secure_link_secret to enable signed URLs)",
            )
        raise AssetAccessNotSupportedError(
            _BACKEND_NAME, "build_download_url (asset not found)"
        )

    def build_upload_url(
        self,
        key: str,
        content_type: str,
        ttl_seconds: int | None = None,
        *,
        visibility: AssetVisibility = AssetVisibility.PRIVATE,
    ) -> UploadUrlResult:
        """Return a tus upload-creation URL pointing to the configured tusd server.

        The returned :class:`UploadUrlResult` carries ``method="POST"`` and
        the required tus headers.  The upload flow is:

        1. Client sends ``POST {url}`` with the headers from ``result.headers``
           and ``Content-Length: 0`` (tus creation request).
        2. tusd calls your *pre-create* hook to validate ``upload-token``.
        3. Client sends ``PATCH {location}`` chunks until the upload is complete.
        4. tusd calls your *post-finish* hook to move the file into
           ``{storage_path}/{visibility_prefix}/{key}``.

        Upload-Metadata fields embedded in the request:

        * ``asset-key``       — logical key for the asset.
        * ``content-type``    — MIME type.
        * ``visibility``      — ``"public"`` or ``"private"``.
        * ``upload-expires``  — Unix timestamp when the token expires.
        * ``upload-token``    — HMAC-SHA256 (hex) of
          ``"{expires}:{key}:{visibility}:{content_type}"`` signed with
          ``upload_secret``.

        Args:
            key:         Logical key for the asset (no leading slash).
            content_type: MIME type of the file to be uploaded.
            ttl_seconds: Override the default ``upload_ttl_seconds`` for this
                         call only.  Governs how long the token is valid.
            visibility:  Target visibility for the asset (keyword-only).
                         Defaults to ``AssetVisibility.PRIVATE``.

        Returns:
            :class:`UploadUrlResult` with ``method="POST"`` and
            tus-specific headers.

        Raises:
            AssetAccessNotSupportedError: If ``tusd_url`` or ``upload_secret``
                is not configured.
        """
        _assert_no_leading_slash(key)
        if not self._cfg.tusd_url:
            raise AssetAccessNotSupportedError(
                _BACKEND_NAME,
                "build_upload_url (tusd_url not configured"
                " — set tusd_url to enable tus uploads)",
            )
        if not self._cfg.upload_secret:
            raise AssetAccessNotSupportedError(
                _BACKEND_NAME,
                "build_upload_url (upload_secret not configured"
                " — set upload_secret to sign upload tokens)",
            )

        ttl = ttl_seconds if ttl_seconds is not None else self._cfg.upload_ttl_seconds
        expires = int(time.time()) + ttl
        token = self._build_upload_token(key, visibility, content_type, expires)
        metadata = self._tus_metadata_header(
            key, visibility, content_type, token, expires
        )
        url = self._cfg.tusd_url.rstrip("/") + "/files/"

        return UploadUrlResult(
            url=url,
            method="POST",
            headers={
                "Tus-Resumable": "1.0.0",
                "Upload-Metadata": metadata,
                "Content-Length": "0",
            },
            expires_at=datetime.fromtimestamp(expires, tz=UTC),
            key=key,
        )

    def resolve_access(
        self, key: str, ttl_seconds: int | None = None
    ) -> AssetAccessUrl:
        """Return the best available URL for the asset.

        * **Public assets** → permanent public URL.
        * **Private assets with ``secure_link_secret`` configured** → signed
          URL (same as :meth:`build_download_url`).
        * **Private assets without ``secure_link_secret``** → raises
          :exc:`AssetAccessNotSupportedError`.

        Args:
            key:         Logical key of the asset (no leading slash).
            ttl_seconds: TTL override forwarded to the signed-URL builder.

        Raises:
            AssetNotFoundError:           If the key does not exist under any
                                          visibility prefix.
            AssetAccessNotSupportedError: If the asset is private and
                                          ``secure_link_secret`` is not set.
        """
        _assert_no_leading_slash(key)
        pub_path = self._full_path(key, AssetVisibility.PUBLIC)
        if pub_path.exists():
            return self.build_public_url(key)

        priv_path = self._full_path(key, AssetVisibility.PRIVATE)
        if priv_path.exists():
            if self._cfg.secure_link_secret:
                return self._build_signed_private_url(key, ttl_seconds)
            raise AssetAccessNotSupportedError(
                _BACKEND_NAME,
                "resolve_access (private asset"
                " — set secure_link_secret to enable signed URLs)",
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
_: IAssetRepository = LocalNginxAssetRepository.__new__(LocalNginxAssetRepository)
