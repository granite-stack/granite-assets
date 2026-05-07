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

from granite_assets.enums import AssetVisibility, CfSigningMethod

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
    """Result of a pre-signed or tus upload URL request.

    Backends may use different upload protocols:

    * **S3 / S3-compatible** — presigned ``PUT`` (single-part, up to 5 GB).  The
      client sends the file body directly as a plain HTTP PUT to *url*.
    * **tusd (tus protocol)** — the client sends an HTTP ``POST`` to *url* to
      *create* the upload resource, then ``PATCH`` chunks until complete.  The
      required tus headers (``Tus-Resumable``, ``Upload-Metadata``) are included
      in *headers*.  This supports arbitrarily large files and resumable uploads.

    Attributes:
        url:         The URL the client should target for the first request.
        method:      HTTP method for the first request (``"PUT"`` for S3,
                     ``"POST"`` for tus).
        headers:     Headers that the client *must* include in the request.
        expires_at:  When the upload authorisation token expires.
        key:         The logical key that will be created after a successful upload.
    """

    url: str
    method: str
    headers: dict[str, str]
    expires_at: datetime
    key: str


@dataclass(slots=True, frozen=True)
class CfSignedCookies:
    """CloudFront signed-cookie values to be set on the response.

    All three cookies are required by CloudFront.  Set them with
    ``HttpOnly; Secure; SameSite=None`` so the browser sends them on
    cross-origin CloudFront requests.

    Attributes:
        policy:       Base64-encoded custom policy (``CloudFront-Policy`` cookie).
        signature:    RSA-SHA1 signature of the policy (``CloudFront-Signature``).
        key_pair_id:  CloudFront key-pair ID (``CloudFront-Key-Pair-Id``).
        expires_at:   When the authorisation expires.
    """

    policy: str
    signature: str
    key_pair_id: str
    expires_at: datetime

    def as_cookie_header_values(self) -> dict[str, str]:
        """Return a mapping of cookie name → value ready for ``Set-Cookie``."""
        return {
            "CloudFront-Policy": self.policy,
            "CloudFront-Signature": self.signature,
            "CloudFront-Key-Pair-Id": self.key_pair_id,
        }




@dataclass(slots=True)
class LocalNginxAssetRepositoryConfig:
    """Configuration for :class:`LocalNginxAssetRepository`.

    Attributes:
        storage_path:       Absolute path on disk where assets are written.
        base_url:           Root URL at which Nginx (or any static server) serves
                            the ``storage_path`` directory.  Example::

                                "http://localhost:8080/assets"

        public_prefix:      Sub-directory name (and URL path segment) used for
                            publicly accessible assets.  Default: ``"public"``.
                            Files are placed at
                            ``{storage_path}/{public_prefix}/{key}`` and served
                            at ``{base_url}/{public_prefix}/{key}``.

        private_prefix:     Sub-directory name for private assets.
                            Default: ``"private"``.  Files are placed at
                            ``{storage_path}/{private_prefix}/{key}`` and are
                            only served when Nginx signed-URL validation passes
                            (requires ``secure_link_secret`` to be set) or when
                            the operator configures ``auth_request`` / internal
                            directives on their own.

        overwrite:          Global overwrite default applied when
                            :attr:`AssetSaveRequest.overwrite` is *None*.
                            Defaults to ``True``.

        create_directories: Automatically create missing parent directories when
                            saving assets.  Disable if you want strict control
                            over the directory layout.  Default: ``True``.

        secure_link_secret: Shared secret used to generate and validate
                            Nginx ``secure_link`` tokens for **private** assets.
                            When set, :meth:`build_download_url` and
                            :meth:`resolve_access` will return time-limited
                            signed URLs instead of raising
                            :exc:`AssetAccessNotSupportedError`.

                            The secret must match the value configured in the
                            Nginx directive::

                                secure_link_md5 "$secure_link_expires$uri SECRET";

                            **Keep this value secret.**  Anyone who knows it can
                            forge download tokens.  Use an environment variable
                            or a secrets manager — never hardcode it.

                            When *None* (default), private asset access via URL
                            is unsupported and the caller must proxy downloads
                            through the application layer.

        secure_link_ttl_seconds: Default lifetime (in seconds) of signed URLs
                            generated for private assets.  Default: ``3600``
                            (1 hour).  Individual calls to
                            :meth:`build_download_url` can override this value
                            via the *ttl_seconds* parameter.

        tusd_url:           Base URL of the `tusd <https://github.com/tus/tusd>`_
                            server used to receive file uploads (e.g.
                            ``"http://localhost:1080"``).  When set,
                            :meth:`build_upload_url` generates a tus creation
                            request targeting ``{tusd_url}/files/``.

                            tusd must be configured to call back to your
                            application's hook endpoint so it can:

                            1. Validate the ``upload-token`` in the upload
                               metadata (*pre-create* hook).
                            2. Move the finished upload to the correct directory
                               under *storage_path* (*post-finish* hook).

                            When *None* (default), :meth:`build_upload_url`
                            raises :exc:`AssetAccessNotSupportedError`.

        upload_secret:      HMAC-SHA256 secret used to sign upload tokens
                            embedded in the tus ``Upload-Metadata`` header.
                            The same secret must be available to the hook
                            endpoint that validates incoming uploads.

                            **Keep this value secret** — it authorises writes
                            to your storage directory.

                            When *None* and *tusd_url* is set, any upload is
                            accepted without verification (only appropriate
                            behind a trusted network).

        upload_ttl_seconds: Default lifetime (in seconds) of upload tokens.
                            Default: ``3600`` (1 hour).  Individual calls to
                            :meth:`build_upload_url` can override this value.
    """

    storage_path: str
    base_url: str
    public_prefix: str = "public"
    private_prefix: str = "private"
    overwrite: bool = True
    create_directories: bool = True
    secure_link_secret: str | None = None
    secure_link_ttl_seconds: int = 3600
    tusd_url: str | None = None
    upload_secret: str | None = None
    upload_ttl_seconds: int = 3600


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
    cf_key_id: str | None = None
    """CloudFront key-pair ID (``KXXXXXXXXXXXXX``). When set together with
    ``cf_private_key``, :meth:`build_download_url` generates a CloudFront
    signed URL instead of an S3 presigned URL."""
    cf_private_key: str | None = None
    """PEM-encoded RSA private key matching ``cf_key_id``. May be the raw PEM
    string (``-----BEGIN RSA PRIVATE KEY-----\\n...``) or a path to a file
    (not recommended; prefer injecting the value from a secret manager)."""
    cf_unsigned_urls: bool = False
    """When ``True`` **and** ``public_base_url`` is set, :meth:`build_download_url`
    returns a plain CloudFront URL (no signature, no expiry) instead of an S3
    presigned URL.  Use this when the CloudFront distribution has no *Restrict
    Viewer Access* policy and the bucket is only accessible via CloudFront OAC.
    The S3 bucket remains private; the URL is permanent (``expires_at=None``).

    Security trade-off: the URL does not expire — anyone who obtains it can
    access the asset indefinitely.  For time-limited access set ``cf_key_id``
    and ``cf_private_key`` instead.

    ``has_cf_signing()`` takes precedence: if both signing keys and this flag
    are set, CloudFront signed URLs are used."""
    cf_signing_method: CfSigningMethod = CfSigningMethod.URL
    """Controls whether access credentials are embedded in the URL (default) or
    issued as signed cookies.  See :class:`~granite_assets.enums.CfSigningMethod`.

    - ``URL``    – :meth:`build_download_url` returns a CloudFront signed URL
      (query-param credentials).  Works everywhere, no cookie handling needed.
    - ``COOKIE`` – :meth:`build_download_url` returns a plain (unsigned)
      CloudFront URL.  The caller must first obtain cookies via
      :meth:`build_signed_cookies` and set them on the browser.
    """

    def presign_ttl(self) -> timedelta:
        """Convenience accessor returning the TTL as a :class:`timedelta`."""
        return timedelta(seconds=self.presign_ttl_seconds)

    def has_cf_signing(self) -> bool:
        """Return True when both CloudFront signing fields are present."""
        return bool(self.cf_key_id and self.cf_private_key)

    def has_cf_unsigned_url(self) -> bool:
        """Return True when plain (unsigned) CloudFront URL delivery is active.

        Requires ``cf_unsigned_urls=True`` *and* ``public_base_url`` to be set.
        ``has_cf_signing()`` takes precedence over this flag.
        """
        return self.cf_unsigned_urls and bool(self.public_base_url)
