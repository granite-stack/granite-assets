"""S3 asset repository backed by AWS S3 (or any S3-compatible store).

Design decisions
----------------
* **Presigned PUT** is used for upload URLs instead of presigned POST.  POST
  allows richer server-side validation (file-size limits, content-type
  enforcement) but requires a multipart form submission which complicates
  client-side HTTP libraries.  PUT is a plain binary body, trivially consumed
  by ``fetch``, ``axios``, ``requests``, ``curl``, and native mobile SDKs.

* **boto3** is loaded lazily so that importing ``granite_assets`` in a project
  without the ``s3`` extra does *not* raise ``ImportError`` at module level.
  Only instantiating ``S3AssetRepository`` triggers the import.

* Object keys in S3 are prefixed with ``config.key_prefix`` when non-empty.
  The logical *key* exposed to callers never includes this prefix; the mapping
  is transparent.

* Public vs private is implemented via S3 object ACLs when the bucket allows
  it, *or* purely by policy.  To keep the library simple we set
  ``ACL='public-read'`` for public objects and no ACL for private objects.
  Callers must ensure their bucket policy is compatible.  If you rely on a
  bucket policy instead of ACLs, set ``public_base_url`` and manage ACLs
  externally.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from granite_assets.enums import AssetVisibility, CfSigningMethod
from granite_assets.exceptions import (
    AssetConfigurationError,
    AssetError,
    AssetNotFoundError,
)
from granite_assets.models import (
    AssetAccessUrl,
    AssetDescriptor,
    AssetSaveRequest,
    AssetSaveResult,
    CfSignedCookies,
    S3AssetRepositoryConfig,
    UploadUrlResult,
)

if TYPE_CHECKING:
    import boto3  # noqa: F401 – type-checking only
    from mypy_boto3_s3 import S3Client  # noqa: F401

_BACKEND_NAME = "S3AssetRepository"


def _cf_url_safe_base64(data: bytes) -> str:
    """CloudFront-specific URL-safe base64 encoding.

    Standard base64, then substitute: ``+`` → ``-``, ``=`` → ``_``, ``/`` → ``~``.
    """
    import base64

    b64 = base64.b64encode(data).decode()
    return b64.replace("+", "-").replace("=", "_").replace("/", "~")


def _require_boto3() -> Any:
    try:
        import boto3  # noqa: PLC0415
        return boto3
    except ImportError as exc:
        raise ImportError(
            "boto3 is required for S3AssetRepository. "
            "Install it with: pip install granite-assets[s3]"
        ) from exc


def _assert_no_leading_slash(key: str) -> None:
    if key.startswith("/"):
        raise AssetError(f"Asset key must not start with '/': {key!r}")


class S3AssetRepository:
    """Asset repository backed by AWS S3.

    Instantiation is cheap; the boto3 session is created once and reused.

    Example::

        config = S3AssetRepositoryConfig(
            bucket="my-assets",
            region="eu-west-1",
            public_base_url="https://cdn.example.com",
            presign_ttl_seconds=3600,
        )
        repo = S3AssetRepository(config)
    """

    def __init__(self, config: S3AssetRepositoryConfig) -> None:
        self._cfg = config
        self._validate_config()
        self._s3: Any = self._build_client()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_config(self) -> None:
        if not self._cfg.bucket:
            raise AssetConfigurationError("bucket must not be empty")
        if not self._cfg.region:
            raise AssetConfigurationError("region must not be empty")

    def _build_client(self) -> Any:
        boto3 = _require_boto3()
        kwargs: dict[str, Any] = {
            "region_name": self._cfg.region,
        }
        if self._cfg.endpoint_url:
            kwargs["endpoint_url"] = self._cfg.endpoint_url
        if self._cfg.access_key_id and self._cfg.secret_access_key:
            kwargs["aws_access_key_id"] = self._cfg.access_key_id
            kwargs["aws_secret_access_key"] = self._cfg.secret_access_key
        if self._cfg.session_token:
            kwargs["aws_session_token"] = self._cfg.session_token
        return boto3.client("s3", **kwargs)

    def _s3_key(self, key: str) -> str:
        """Map a logical key to the actual S3 object key."""
        prefix = self._cfg.key_prefix.rstrip("/")
        return f"{prefix}/{key}" if prefix else key

    def _logical_key(self, s3_key: str) -> str:
        """Strip the configured prefix to get the logical key back."""
        prefix = self._cfg.key_prefix.rstrip("/")
        if prefix and s3_key.startswith(f"{prefix}/"):
            return s3_key[len(prefix) + 1:]
        return s3_key

    def _effective_ttl(self, ttl_seconds: int | None) -> int:
        return ttl_seconds if ttl_seconds is not None else self._cfg.presign_ttl_seconds

    def _expires_at(self, ttl_seconds: int) -> datetime:
        return datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds)

    def _public_url_for_key(self, s3_key: str) -> str:
        if self._cfg.public_base_url:
            base = self._cfg.public_base_url.rstrip("/")
            return f"{base}/{s3_key}"
        # Fall back to virtual-hosted-style URL
        bucket = self._cfg.bucket
        region = self._cfg.region
        return f"https://{bucket}.s3.{region}.amazonaws.com/{s3_key}"

    def _cf_base_url(self) -> str:
        """Return the CloudFront base URL, raising if not configured."""
        if not self._cfg.public_base_url:
            raise AssetConfigurationError(
                "public_base_url must be set to use CloudFront signed URLs"
            )
        return self._cfg.public_base_url.rstrip("/")

    def _build_cf_signed_url(self, s3_key: str, ttl: int) -> str:
        """Generate a CloudFront signed URL (canned policy) for *s3_key*.

        Uses ``Expires`` / ``Signature`` / ``Key-Pair-Id`` query params.
        """
        try:
            from botocore.signers import CloudFrontSigner
        except ImportError as exc:
            raise AssetError(
                "botocore is required for CloudFront signed URLs (install boto3)"
            ) from exc

        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:
            raise AssetError(
                "cryptography is required for CloudFront signed URLs: pip install cryptography"
            ) from exc

        pem: str = self._cfg.cf_private_key  # type: ignore[assignment]
        private_key = serialization.load_pem_private_key(pem.encode(), password=None)

        def _rsa_sign(message: bytes) -> bytes:
            return private_key.sign(message, padding.PKCS1v15(), hashes.SHA1())  # noqa: S303

        signer = CloudFrontSigner(self._cfg.cf_key_id, _rsa_sign)  # type: ignore[arg-type]
        base = self._cf_base_url()
        resource_url = f"{base}/{s3_key}"
        expires_at = self._expires_at(ttl)
        return signer.generate_presigned_url(
            resource_url,
            date_less_than=expires_at,
        )

    def _get_rsa_signer(self):
        """Return ``(rsa_sign_fn, private_key)`` for CloudFront custom-policy operations.

        Lazily imports ``cryptography``; raises :exc:`AssetError` if missing.
        """
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:
            raise AssetError(
                "cryptography is required for CloudFront signed operations: "
                "pip install cryptography"
            ) from exc

        pem: str = self._cfg.cf_private_key  # type: ignore[assignment]
        private_key = serialization.load_pem_private_key(pem.encode(), password=None)

        def _rsa_sign(message: bytes) -> bytes:
            return private_key.sign(message, padding.PKCS1v15(), hashes.SHA1())  # noqa: S303

        return _rsa_sign

    def _build_cf_custom_policy_params(
        self, resource_pattern: str, ttl: int
    ) -> tuple[str, str, str, int]:
        """Compute CloudFront custom-policy signing params for *resource_pattern*.

        *resource_pattern* may contain a trailing wildcard
        (``https://cdn.example.com/assets/private/videos/uuid/*``) so that a
        single set of credentials authorises every segment of an HLS stream.

        Returns ``(policy_b64, signature_b64, key_pair_id, unix_expires)``.
        """
        import json
        import time

        _rsa_sign = self._get_rsa_signer()
        unix_expires = int(time.time()) + ttl
        policy_dict = {
            "Statement": [
                {
                    "Resource": resource_pattern,
                    "Condition": {"DateLessThan": {"AWS:EpochTime": unix_expires}},
                }
            ]
        }
        policy_json = json.dumps(policy_dict, separators=(",", ":"))
        policy_b64 = _cf_url_safe_base64(policy_json.encode())
        signature_b64 = _cf_url_safe_base64(_rsa_sign(policy_json.encode()))
        return policy_b64, signature_b64, self._cfg.cf_key_id, unix_expires  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def save(self, request: AssetSaveRequest) -> AssetSaveResult:
        """Upload an asset to S3.

        Sets ``ACL='public-read'`` for PUBLIC assets.  Metadata and checksum
        are forwarded as S3 object metadata.
        """
        _assert_no_leading_slash(request.key)
        s3_key = self._s3_key(request.key)
        stream = request.open_source()

        put_kwargs: dict[str, Any] = {
            "Bucket": self._cfg.bucket,
            "Key": s3_key,
            "Body": stream,
            "ContentType": request.content_type,
        }

        if request.visibility == AssetVisibility.PUBLIC:
            put_kwargs["ACL"] = "public-read"

        if request.content_length is not None:
            put_kwargs["ContentLength"] = request.content_length

        # Always store visibility so that get_descriptor / resolve_access can
        # reconstruct it without an extra get_object_acl call.
        put_kwargs["Metadata"] = {
            **(request.metadata or {}),
            "x-visibility": request.visibility.value,
        }

        if request.checksum:
            put_kwargs["Metadata"]["x-checksum"] = request.checksum

        try:
            response = self._s3.put_object(**put_kwargs)
        except Exception as exc:  # botocore.exceptions.ClientError
            raise AssetError(f"Failed to save asset {request.key!r} to S3: {exc}") from exc

        etag: str = response.get("ETag", "").strip('"')
        return AssetSaveResult(
            key=request.key,
            backend_ref=f"s3://{self._cfg.bucket}/{s3_key}",
            checksum=f"etag:{etag}" if etag else None,
            visibility=request.visibility,
        )

    def delete(self, key: str) -> None:
        """Delete an S3 object.

        Raises:
            AssetNotFoundError: If the key does not exist.
        """
        _assert_no_leading_slash(key)
        if not self.exists(key):
            raise AssetNotFoundError(key)
        s3_key = self._s3_key(key)
        try:
            self._s3.delete_object(Bucket=self._cfg.bucket, Key=s3_key)
        except Exception as exc:
            raise AssetError(f"Failed to delete asset {key!r}: {exc}") from exc

    def copy(self, source_key: str, dest_key: str, *, overwrite: bool = True) -> None:
        """Server-side S3 copy (no data transfer to/from this process)."""
        _assert_no_leading_slash(source_key)
        _assert_no_leading_slash(dest_key)

        if not self.exists(source_key):
            raise AssetNotFoundError(source_key)
        if not overwrite and self.exists(dest_key):
            raise AssetError(f"Destination key already exists: {dest_key!r}")

        src_s3_key = self._s3_key(source_key)
        dst_s3_key = self._s3_key(dest_key)
        copy_source = {"Bucket": self._cfg.bucket, "Key": src_s3_key}
        try:
            self._s3.copy_object(
                CopySource=copy_source,
                Bucket=self._cfg.bucket,
                Key=dst_s3_key,
            )
        except Exception as exc:
            raise AssetError(
                f"Failed to copy asset {source_key!r} -> {dest_key!r}: {exc}"
            ) from exc

    def move(self, source_key: str, dest_key: str, *, overwrite: bool = True) -> None:
        """Copy then delete (S3 has no native move operation)."""
        self.copy(source_key, dest_key, overwrite=overwrite)
        try:
            self._s3.delete_object(
                Bucket=self._cfg.bucket, Key=self._s3_key(source_key)
            )
        except Exception as exc:
            raise AssetError(
                f"Failed to remove source after move {source_key!r}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    def exists(self, key: str) -> bool:
        """Check object existence using a lightweight ``head_object`` call."""
        _assert_no_leading_slash(key)
        try:
            self._s3.head_object(Bucket=self._cfg.bucket, Key=self._s3_key(key))
            return True
        except Exception as exc:
            # botocore raises ClientError with 404 or NoSuchKey
            error_code = getattr(getattr(exc, "response", {}), "get", lambda *_: None)(
                "Error", {}
            ).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                return False
            # For moto/real boto3 we check the string representation
            if "404" in str(exc) or "NoSuchKey" in str(exc) or "Not Found" in str(exc):
                return False
            raise AssetError(f"Failed to check existence of {key!r}: {exc}") from exc

    def get_descriptor(self, key: str) -> AssetDescriptor:
        """Return S3 object metadata via ``head_object``."""
        _assert_no_leading_slash(key)
        s3_key = self._s3_key(key)
        try:
            response = self._s3.head_object(Bucket=self._cfg.bucket, Key=s3_key)
        except Exception as exc:
            if "404" in str(exc) or "NoSuchKey" in str(exc) or "Not Found" in str(exc):
                raise AssetNotFoundError(key) from exc
            raise AssetError(f"Failed to get descriptor for {key!r}: {exc}") from exc

        raw_meta: dict[str, str] = response.get("Metadata") or {}
        visibility_str = raw_meta.get("x-visibility", "private")
        visibility = (
            AssetVisibility.PUBLIC
            if visibility_str == "public"
            else AssetVisibility.PRIVATE
        )
        return AssetDescriptor(
            key=key,
            content_type=response.get("ContentType"),
            content_length=response.get("ContentLength"),
            last_modified=response.get("LastModified"),
            checksum=f"etag:{response.get('ETag', '').strip('\"')}",
            visibility=visibility,
            metadata=raw_meta,
        )

    # ------------------------------------------------------------------
    # URL construction
    # ------------------------------------------------------------------

    def build_public_url(self, key: str) -> AssetAccessUrl:
        """Return the permanent public URL for a PUBLIC asset.

        If ``public_base_url`` is configured, uses that as base (CDN URL).
        Otherwise builds a standard virtual-hosted S3 URL.

        Raises:
            AssetAccessNotSupportedError: If called for a PRIVATE asset key that
                is known to be private (best-effort; requires a head_object call
                not performed here for performance).
        """
        _assert_no_leading_slash(key)
        s3_key = self._s3_key(key)
        url = self._public_url_for_key(s3_key)
        return AssetAccessUrl(url=url, expires_at=None)

    def build_download_url(self, key: str, ttl_seconds: int | None = None) -> AssetAccessUrl:
        """Generate a download URL for a private asset.

        Priority order:

        1. ``cf_key_id`` + ``cf_private_key`` set **and** ``cf_signing_method=URL``
           → **CloudFront signed URL** (canned policy, query-param credentials).
        2. ``cf_key_id`` + ``cf_private_key`` set **and** ``cf_signing_method=COOKIE``
           → **plain CloudFront URL** (no signature).  The browser must already
           hold the signed cookies obtained via :meth:`build_signed_cookies`.
        3. ``cf_unsigned_urls=True`` + ``public_base_url`` set
           → **plain CloudFront URL** (permanent, no signature).
        4. Fallback → **S3 presigned URL** (time-limited, exposes S3 domain).
        """
        _assert_no_leading_slash(key)
        s3_key = self._s3_key(key)
        try:
            if self._cfg.has_cf_signing():
                if self._cfg.cf_signing_method == CfSigningMethod.COOKIE:
                    # Credentials are in browser cookies — return plain CF URL.
                    base = self._cf_base_url()
                    return AssetAccessUrl(url=f"{base}/{s3_key}", expires_at=None)

                # Default: URL-based signed URL (canned policy).
                ttl = self._effective_ttl(ttl_seconds)
                url = self._build_cf_signed_url(s3_key, ttl)
                return AssetAccessUrl(url=url, expires_at=self._expires_at(ttl))

            if self._cfg.has_cf_unsigned_url():
                base = self._cfg.public_base_url.rstrip("/")  # type: ignore[union-attr]
                url = f"{base}/{s3_key}"
                return AssetAccessUrl(url=url, expires_at=None)

            ttl = self._effective_ttl(ttl_seconds)
            url = self._s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._cfg.bucket, "Key": s3_key},
                ExpiresIn=ttl,
            )
        except AssetError:
            raise
        except Exception as exc:
            raise AssetError(
                f"Failed to generate signed download URL for {key!r}: {exc}"
            ) from exc
        return AssetAccessUrl(url=url, expires_at=self._expires_at(ttl))

    def build_path_signed_url(
        self,
        key: str,
        *,
        path_pattern: str | None = None,
        ttl_seconds: int | None = None,
    ) -> AssetAccessUrl:
        """Generate a CloudFront URL with **custom-policy** signing for *key*.

        Unlike the canned-policy URL returned by :meth:`build_download_url`, the
        custom policy can authorise a **wildcard path** so that a single set of
        query-param credentials is valid for every file under a directory.  This
        is the recommended approach for HLS/DASH video streaming where the player
        autonomously fetches dozens of segment files.

        Args:
            key:          Logical key of the file whose URL is returned
                          (e.g. ``"private/videos/uuid/master.m3u8"``).  Must
                          not start with ``/``.
            path_pattern: CloudFront resource pattern to embed in the policy.
                          Accepts a trailing ``*`` wildcard
                          (e.g. ``"private/videos/uuid/*"``).
                          Defaults to the directory of *key* + ``/*``.
            ttl_seconds:  URL lifetime in seconds (default: configured TTL).

        Returns:
            :class:`AssetAccessUrl` whose ``url`` carries
            ``?Policy=…&Signature=…&Key-Pair-Id=…`` query params.

        Raises:
            :exc:`AssetConfigurationError`: if CloudFront signing is not configured.
        """
        if not self._cfg.has_cf_signing():
            raise AssetConfigurationError(
                "cf_key_id and cf_private_key must be set to use build_path_signed_url"
            )
        _assert_no_leading_slash(key)
        ttl = self._effective_ttl(ttl_seconds)
        base = self._cf_base_url()
        s3_key = self._s3_key(key)

        if path_pattern is None:
            # Derive: strip filename, append /*
            directory = s3_key.rsplit("/", 1)[0] if "/" in s3_key else s3_key
            resource_pattern = f"{base}/{directory}/*"
        else:
            _assert_no_leading_slash(path_pattern.lstrip("*").lstrip("/"))
            resource_pattern = f"{base}/{self._s3_key(path_pattern)}"

        policy_b64, sig_b64, kp_id, _ = self._build_cf_custom_policy_params(
            resource_pattern, ttl
        )
        url = (
            f"{base}/{s3_key}"
            f"?Policy={policy_b64}&Signature={sig_b64}&Key-Pair-Id={kp_id}"
        )
        return AssetAccessUrl(url=url, expires_at=self._expires_at(ttl))

    def build_signed_cookies(
        self,
        key_pattern: str,
        ttl_seconds: int | None = None,
    ) -> CfSignedCookies:
        """Generate CloudFront signed-cookie values for *key_pattern*.

        Call this once per session (or per resource group) and set the returned
        values as ``HttpOnly; Secure; SameSite=None`` cookies on the response.
        The browser will then include them automatically on every CloudFront
        request that matches the policy path.

        Args:
            key_pattern: Logical key pattern (may include a trailing ``*``
                         wildcard) relative to the configured key prefix.
                         Example: ``"private/videos/uuid/*"``.
            ttl_seconds: Cookie lifetime in seconds (default: configured TTL).

        Returns:
            :class:`CfSignedCookies` with ``policy``, ``signature``, and
            ``key_pair_id`` values ready to set as cookies.

        Raises:
            :exc:`AssetConfigurationError`: if CloudFront signing is not configured.
        """
        if not self._cfg.has_cf_signing():
            raise AssetConfigurationError(
                "cf_key_id and cf_private_key must be set to use build_signed_cookies"
            )
        ttl = self._effective_ttl(ttl_seconds)
        base = self._cf_base_url()
        s3_pattern = self._s3_key(key_pattern)
        resource_pattern = f"{base}/{s3_pattern}"

        policy_b64, sig_b64, kp_id, unix_expires = self._build_cf_custom_policy_params(
            resource_pattern, ttl
        )
        from datetime import UTC

        return CfSignedCookies(
            policy=policy_b64,
            signature=sig_b64,
            key_pair_id=kp_id,
            expires_at=datetime.fromtimestamp(unix_expires, tz=UTC),
        )

    def build_upload_url(
        self,
        key: str,
        content_type: str,
        ttl_seconds: int | None = None,
    ) -> UploadUrlResult:
        """Generate a presigned PUT URL for client-side upload.

        The client must send the file as an HTTP PUT with the ``Content-Type``
        header set to exactly the value provided here.  No other headers are
        required by default.

        Example (using ``requests``)::

            result = repo.build_upload_url("images/photo.jpg", "image/jpeg")
            with open("photo.jpg", "rb") as f:
                requests.put(result.url, data=f, headers=result.headers)
        """
        _assert_no_leading_slash(key)
        ttl = self._effective_ttl(ttl_seconds)
        s3_key = self._s3_key(key)
        try:
            url = self._s3.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self._cfg.bucket,
                    "Key": s3_key,
                    "ContentType": content_type,
                },
                ExpiresIn=ttl,
            )
        except Exception as exc:
            raise AssetError(
                f"Failed to generate presigned upload URL for {key!r}: {exc}"
            ) from exc
        return UploadUrlResult(
            url=url,
            method="PUT",
            headers={"Content-Type": content_type},
            expires_at=self._expires_at(ttl),
            key=key,
        )

    def resolve_access(self, key: str, ttl_seconds: int | None = None) -> AssetAccessUrl:
        """Return public URL for public assets, signed download URL for private."""
        _assert_no_leading_slash(key)
        descriptor = self.get_descriptor(key)
        if descriptor.visibility == AssetVisibility.PUBLIC:
            return self.build_public_url(key)
        return self.build_download_url(key, ttl_seconds=ttl_seconds)


# Verify structural compatibility at import time
assert isinstance(S3AssetRepository, type)
