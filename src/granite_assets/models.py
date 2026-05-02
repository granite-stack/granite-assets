"""Domain models and configuration dataclasses for granite-assets.

All models use ``slots=True`` for memory efficiency and to prevent accidental
attribute creation. ``frozen=True`` is applied to value objects that should
be immutable after construction; mutable builder-style objects stay unfrozen.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import BinaryIO

from granite_assets.enums import AssetVisibility


# ---------------------------------------------------------------------------
# Input / request models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AssetSaveRequest:
    """All the information needed to persist a new asset.

    Attributes:
        key:          Logical key that uniquely identifies the asset within the
                      repository (e.g. ``"invoices/2024/inv-001.pdf"``).  It must
                      not contain a leading slash.
        source:       Readable binary stream with the asset content.
        content_type: MIME type (e.g. ``"application/pdf"``).
        visibility:   Whether the asset will be publicly accessible or private.
        filename:     Original human-readable filename, stored as metadata.
        content_length: Byte size of the asset when known; used to set
                      ``Content-Length`` headers on upload.
        checksum:     Optional integrity hash (e.g. ``"md5:abc123"``).
        metadata:     Arbitrary key/value pairs forwarded to the backend (e.g.
                      S3 object metadata or extended file attributes).
        overwrite:    If *False* and the key already exists the backend should
                      raise ``AssetError``.  Implementations may expose a
                      per-request override even when the global config differs.
    """

    key: str
    source: BinaryIO | bytes
    content_type: str
    visibility: AssetVisibility = AssetVisibility.PRIVATE
    filename: str | None = None
    content_length: int | None = None
    checksum: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    overwrite: bool = True

    def open_source(self) -> BinaryIO:
        """Return a readable binary stream regardless of whether *source* is
        already a stream or a raw ``bytes`` object."""
        if isinstance(self.source, bytes):
            return io.BytesIO(self.source)
        return self.source


# ---------------------------------------------------------------------------
# Output / result models
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class AssetSaveResult:
    """Returned by :meth:`IAssetRepository.save` after a successful write.

    Attributes:
        key:            The logical key under which the asset was stored.
        backend_ref:    Backend-specific identifier (e.g. S3 ETag, local path).
        content_length: Byte size as recorded by the backend.
        checksum:       Integrity hash as returned or calculated by the backend.
        visibility:     Visibility at the time of saving.
    """

    key: str
    backend_ref: str
    content_length: int | None = None
    checksum: str | None = None
    visibility: AssetVisibility = AssetVisibility.PRIVATE


@dataclass(slots=True, frozen=True)
class AssetDescriptor:
    """Metadata about an asset that already exists in the repository.

    Attributes:
        key:            Logical key.
        content_type:   MIME type.
        content_length: Size in bytes.
        visibility:     Current visibility.
        last_modified:  Last modification timestamp (backend-provided).
        checksum:       Integrity hash when available.
        metadata:       Backend-provided key/value metadata.
    """

    key: str
    content_type: str | None = None
    content_length: int | None = None
    visibility: AssetVisibility = AssetVisibility.PRIVATE
    last_modified: datetime | None = None
    checksum: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class AssetAccessUrl:
    """A resolved URL for accessing an asset.

    Attributes:
        url:        The full URL string.
        expires_at: Expiry timestamp for signed URLs; *None* for permanent URLs.
    """

    url: str
    expires_at: datetime | None = None

    @property
    def is_permanent(self) -> bool:
        """``True`` when the URL has no expiry (i.e. public permanent URL)."""
        return self.expires_at is None


@dataclass(slots=True, frozen=True)
class UploadUrlResult:
    """Result of a pre-signed upload URL request.

    For S3, the chosen mechanism is a **presigned PUT** (single-part upload up
    to 5 GB).  A presigned POST would allow more server-side validation but adds
    complexity for the common case; PUT is simpler to consume from any HTTP
    client and is the idiomatic choice for most REST-style integrations.

    Attributes:
        url:         The pre-signed URL the client should PUT to.
        method:      HTTP method to use (always ``"PUT"`` for this library).
        headers:     Headers that the client *must* include in the request
                     (e.g. ``Content-Type``, ``Content-Length``).
        expires_at:  When the URL stops being valid.
        key:         The logical key that will be created after a successful PUT.
    """

    url: str
    method: str
    headers: dict[str, str]
    expires_at: datetime
    key: str


# ---------------------------------------------------------------------------
# Configuration models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LocalNginxAssetRepositoryConfig:
    """Configuration for :class:`LocalNginxAssetRepository`.

    Attributes:
        storage_path:       Absolute path on disk where assets are written.
        base_url:           Root URL at which Nginx (or any static server) serves
                            the ``storage_path`` directory.
        public_prefix:      Sub-path appended to both the storage path and the
                            URL for public assets (default ``"public"``).
        private_prefix:     Sub-path for private assets.  Since local Nginx has no
                            signed-URL support these files can only be served if
                            you protect the directory at the Nginx level.
        overwrite:          Global overwrite default; can be overridden per request.
        create_directories: Automatically create missing intermediate directories.
    """

    storage_path: str
    base_url: str
    public_prefix: str = "public"
    private_prefix: str = "private"
    overwrite: bool = True
    create_directories: bool = True


@dataclass(slots=True)
class S3AssetRepositoryConfig:
    """Configuration for :class:`S3AssetRepository`.

    Attributes:
        bucket:           S3 bucket name.
        region:           AWS region (e.g. ``"eu-west-1"``).
        public_base_url:  Optional CDN or custom-domain base URL for public assets.
                          When set, ``build_public_url`` will use it instead of the
                          native S3 endpoint.
        key_prefix:       Optional prefix prepended to every logical key before
                          writing to S3 (e.g. ``"uploads/"``).
        presign_ttl_seconds: Default TTL for presigned URLs.
        endpoint_url:     Custom endpoint for S3-compatible stores (MinIO, etc.).
        access_key_id:    Explicit AWS credentials (optional; falls back to the
                          standard boto3 credential chain).
        secret_access_key: Explicit AWS credentials.
        session_token:    STS session token when using temporary credentials.
    """

    bucket: str
    region: str
    public_base_url: str | None = None
    key_prefix: str = ""
    presign_ttl_seconds: int = 3600
    endpoint_url: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None

    def presign_ttl(self) -> timedelta:
        """Convenience accessor returning the TTL as a :class:`timedelta`."""
        return timedelta(seconds=self.presign_ttl_seconds)
