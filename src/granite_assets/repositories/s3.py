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

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from granite_assets._keys import (
    check_content_type,
    check_size,
    quote_key,
    quote_pattern,
    resolve_asset_key,
    validate_key,
)
from granite_assets.contracts import IAssetRepository
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

logger = logging.getLogger(__name__)

_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


def _is_not_found(exc: BaseException) -> bool:
    """Return True only for a genuine "object does not exist" S3 error.

    Decided from the structured ``response`` of a botocore ``ClientError``
    (error code or HTTP status), never from the text of the message.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in _NOT_FOUND_CODES or status == 404


def _validate_pattern(pattern: str) -> None:
    """Validate a CloudFront resource *pattern* given as a logical key path.

    It must be a safe relative path whose literal part is non-empty and that
    only uses a single trailing ``*`` wildcard, so a caller cannot sign a
    pattern that matches the whole distribution (``"*"``).
    """
    literal = pattern[:-1] if pattern.endswith("*") else pattern
    if not literal.strip("/"):
        raise AssetError(f"Signed-URL pattern must not be a bare wildcard: {pattern!r}")
    if "*" in literal or "?" in literal:
        raise AssetError(
            f"Signed-URL pattern may only use a single trailing '*': {pattern!r}"
        )
    validate_key(literal.rstrip("/") if literal.endswith("/") else literal)


def _validate_entry_filename(name: str) -> None:
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise AssetError(f"Invalid entry_filename: {name!r}")
    validate_key(name)


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
    """Validate an asset key (name kept for backward compatibility)."""
    validate_key(key)


_resolve_asset_key = resolve_asset_key


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

    def _credential_kwargs(self) -> dict[str, Any]:
        """Explicit credentials of the config (empty: the boto3 chain applies)."""
        kwargs: dict[str, Any] = {}
        if self._cfg.access_key_id and self._cfg.secret_access_key:
            kwargs["aws_access_key_id"] = self._cfg.access_key_id
            kwargs["aws_secret_access_key"] = self._cfg.secret_access_key
        if self._cfg.session_token:
            kwargs["aws_session_token"] = self._cfg.session_token
        return kwargs

    def _build_client(self, signature_version: str | None = None) -> Any:
        boto3 = _require_boto3()
        kwargs: dict[str, Any] = {
            "region_name": self._cfg.region,
            **self._credential_kwargs(),
        }
        if self._cfg.endpoint_url:
            kwargs["endpoint_url"] = self._cfg.endpoint_url
        if signature_version:
            from botocore.config import Config

            kwargs["config"] = Config(signature_version=signature_version)
        return boto3.client("s3", **kwargs)

    def s3_key(self, key: str) -> str:
        """Map a logical key to the actual S3 object key (public API)."""
        prefix = self._cfg.key_prefix.rstrip("/")
        return f"{prefix}/{key}" if prefix else key

    def _s3_key(self, key: str) -> str:
        """Backward-compatible alias of :meth:`s3_key`."""
        return self.s3_key(key)

    def _effective_ttl(self, ttl_seconds: int | None) -> int:
        return ttl_seconds if ttl_seconds is not None else self._cfg.presign_ttl_seconds

    def _expires_at(self, ttl_seconds: int) -> datetime:
        return datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds)

    def _public_url_for_key(self, s3_key: str) -> str:
        if self._cfg.public_base_url:
            base = self._cfg.public_base_url.rstrip("/")
            return f"{base}/{quote_key(s3_key)}"
        # Fall back to virtual-hosted-style URL
        bucket = self._cfg.bucket
        region = self._cfg.region
        return f"https://{bucket}.s3.{region}.amazonaws.com/{quote_key(s3_key)}"

    def cf_base_url(self) -> str:
        """Return the CloudFront base URL (public API).

        Raises:
            AssetConfigurationError: If ``public_base_url`` is not configured.
        """
        if not self._cfg.public_base_url:
            raise AssetConfigurationError(
                "public_base_url must be set to use CloudFront signed URLs"
            )
        return self._cfg.public_base_url.rstrip("/")

    def cf_object_url(self, key: str) -> str:
        """Return the plain (unsigned, percent-encoded) CloudFront URL of *key*.

        Used e.g. in signed-cookie mode, where credentials travel in cookies.
        """
        validate_key(key)
        return f"{self.cf_base_url()}/{quote_key(self.s3_key(key))}"

    def invalidate_cache(self, *keys: str) -> None:
        """Ask CloudFront to drop its cached copy of *keys*.

        *keys* are logical keys: the ``key_prefix`` is applied.  Use it when a
        publicly cached object stops being public or changes under the same key,
        so the edge does not keep serving the stale copy.  Needs
        ``cf_distribution_id`` and the ``cloudfront:CreateInvalidation``
        permission.

        Raises:
            AssetConfigurationError: if ``cf_distribution_id`` is not configured.
            AssetError: if CloudFront rejects the request.
        """
        if not self._cfg.cf_distribution_id:
            raise AssetConfigurationError(
                "cf_distribution_id must be set to invalidate the CDN cache"
            )
        if not keys:
            return
        paths = []
        for key in keys:
            _assert_no_leading_slash(key)
            paths.append(f"/{quote_key(self._s3_key(key))}")
        try:
            client = _require_boto3().client("cloudfront", **self._credential_kwargs())
            client.create_invalidation(
                DistributionId=self._cfg.cf_distribution_id,
                InvalidationBatch={
                    "Paths": {"Quantity": len(paths), "Items": paths},
                    "CallerReference": uuid.uuid4().hex,
                },
            )
        except Exception as exc:
            raise AssetError(
                f"Failed to invalidate the CDN cache for {len(paths)} path(s): {exc}"
            ) from exc

    def _cf_base_url(self) -> str:
        """Backward-compatible alias of :meth:`cf_base_url`."""
        return self.cf_base_url()

    def _get_rsa_signer(self) -> Callable[[bytes], bytes]:
        """Return the RSA signer for CloudFront custom-policy operations.

        Lazily imports ``cryptography``; raises :exc:`AssetError` if missing.
        """
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding, rsa
        except ImportError as exc:
            raise AssetError(
                "cryptography is required for CloudFront signed operations: "
                "pip install 'granite-assets[cloudfront]'"
            ) from exc

        pem: str = self._cfg.cf_private_key  # type: ignore[assignment]
        private_key = cast(
            rsa.RSAPrivateKey,
            serialization.load_pem_private_key(pem.encode(), password=None),
        )

        def _rsa_sign(message: bytes) -> bytes:
            return private_key.sign(message, padding.PKCS1v15(), hashes.SHA1())  # noqa: S303  # nosec B303 — CloudFront protocol

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
        key = _resolve_asset_key(request.key, request.filename)
        _assert_no_leading_slash(key)
        check_content_type(request.content_type, self._cfg.allowed_content_types)
        check_size(request.content_length, self._cfg.max_upload_bytes)
        if request.overwrite is False and self.exists(key):
            raise AssetError(f"Asset already exists and overwrite is disabled: {key!r}")
        s3_key = self._s3_key(key)
        stream = request.open_source()
        if self._cfg.max_upload_bytes is not None and request.content_length is None:
            stream = self._bounded(stream, self._cfg.max_upload_bytes)

        put_kwargs: dict[str, Any] = {
            "Bucket": self._cfg.bucket,
            "Key": s3_key,
            "Body": stream,
            "ContentType": request.content_type,
        }

        if request.visibility == AssetVisibility.PUBLIC and self._cfg.use_object_acl:
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
        except AssetError:
            raise
        except Exception as exc:  # botocore.exceptions.ClientError
            raise AssetError(f"Failed to save asset {key!r} to S3: {exc}") from exc

        etag: str = response.get("ETag", "").strip('"')

        # put_object does not return ContentLength in its response.
        # Prefer the caller-supplied hint (zero overhead); fall back to a
        # head_object call so the result always carries the real byte count.
        content_length: int | None = request.content_length
        if content_length is None:
            try:
                head = self._s3.head_object(Bucket=self._cfg.bucket, Key=s3_key)
                content_length = head.get("ContentLength")
            except Exception as exc:  # non-fatal: result carries None instead
                logger.debug("head_object after save failed for %r: %s", key, exc)

        return AssetSaveResult(
            key=key,
            backend_ref=f"s3://{self._cfg.bucket}/{s3_key}",
            content_length=content_length,
            checksum=f"etag:{etag}" if etag else None,
            visibility=request.visibility,
        )

    @staticmethod
    def _bounded(stream: Any, limit: int) -> Any:
        """Read *stream* fully into memory, enforcing *limit* bytes.

        Used only when a size cap is configured and the caller did not declare
        ``content_length`` (boto3 needs a seekable body of known size).
        """
        import io

        buf = io.BytesIO()
        total = 0
        for chunk in iter(lambda: stream.read(65536), b""):
            total += len(chunk)
            check_size(total, limit)
            buf.write(chunk)
        buf.seek(0)
        return buf

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

        source = self.get_descriptor(source_key)  # AssetNotFoundError if missing
        if source_key == dest_key:
            return
        if not overwrite and self.exists(dest_key):
            raise AssetError(f"Destination key already exists: {dest_key!r}")

        src_s3_key = self._s3_key(source_key)
        dst_s3_key = self._s3_key(dest_key)
        copy_kwargs: dict[str, Any] = {
            "CopySource": {"Bucket": self._cfg.bucket, "Key": src_s3_key},
            "Bucket": self._cfg.bucket,
            "Key": dst_s3_key,
            # Keep content type and our x-visibility / x-checksum metadata.
            "MetadataDirective": "COPY",
        }
        # copy_object never copies the ACL: re-apply it from the source
        # visibility so a public asset stays publicly readable.
        if source.visibility == AssetVisibility.PUBLIC and self._cfg.use_object_acl:
            copy_kwargs["ACL"] = "public-read"
        try:
            self._s3.copy_object(**copy_kwargs)
        except Exception as exc:
            raise AssetError(
                f"Failed to copy asset {source_key!r} -> {dest_key!r}: {exc}"
            ) from exc

    def move(self, source_key: str, dest_key: str, *, overwrite: bool = True) -> None:
        """Copy then delete (S3 has no native move operation).

        Not atomic: if the final delete fails the copy already exists and an
        :exc:`AssetError` is raised so the caller can retry the cleanup.
        Moving a key onto itself is a no-op.
        """
        if source_key == dest_key:
            _assert_no_leading_slash(source_key)
            if not self.exists(source_key):
                raise AssetNotFoundError(source_key)
            return
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
            if _is_not_found(exc):
                return False
            raise AssetError(f"Failed to check existence of {key!r}: {exc}") from exc

    def get_descriptor(self, key: str) -> AssetDescriptor:
        """Return S3 object metadata via ``head_object``."""
        _assert_no_leading_slash(key)
        s3_key = self._s3_key(key)
        try:
            response = self._s3.head_object(Bucket=self._cfg.bucket, Key=s3_key)
        except Exception as exc:
            if _is_not_found(exc):
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
            checksum=f"etag:{response.get('ETag', '').strip('"')}",
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

    def build_download_url(
        self, key: str, ttl_seconds: int | None = None
    ) -> AssetAccessUrl:
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

        In mode 1 the signature authorises **only this object**.  To authorise a
        whole folder (HLS/DASH) use :meth:`build_path_signed_url` or
        :meth:`build_folder_signed_url`.
        """
        _assert_no_leading_slash(key)
        s3_key = self._s3_key(key)
        try:
            if self._cfg.has_cf_signing():
                if self._cfg.cf_signing_method == CfSigningMethod.COOKIE:
                    # Credentials are in browser cookies — return plain CF URL.
                    return AssetAccessUrl(
                        url=f"{self.cf_base_url()}/{quote_key(s3_key)}",
                        expires_at=None,
                    )

                # Default: URL-based signed URL whose policy authorises exactly
                # this object (no directory wildcard).  Folder-wide access is
                # opt-in via build_path_signed_url / build_folder_signed_url.
                ttl = self._effective_ttl(ttl_seconds)
                base = self.cf_base_url()
                object_url = f"{base}/{quote_key(s3_key)}"
                policy_b64, sig_b64, kp_id, _ = self._build_cf_custom_policy_params(
                    object_url, ttl
                )
                url = (
                    f"{object_url}"
                    f"?Policy={policy_b64}&Signature={sig_b64}&Key-Pair-Id={kp_id}"
                )
                return AssetAccessUrl(url=url, expires_at=self._expires_at(ttl))

            if self._cfg.has_cf_unsigned_url():
                base = self._cfg.public_base_url.rstrip("/")  # type: ignore[union-attr]
                url = f"{base}/{quote_key(s3_key)}"
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

    @staticmethod
    def _pattern_matches(pattern: str, s3_key: str) -> bool:
        if pattern.endswith("*"):
            return s3_key.startswith(pattern[:-1])
        return s3_key == pattern

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
        base = self.cf_base_url()
        s3_key = self._s3_key(key)

        if path_pattern is None:
            if "/" not in s3_key:
                # Root-level object: authorise just that object, never the
                # whole distribution.
                resource_pattern = f"{base}/{quote_key(s3_key)}"
            else:
                directory = s3_key.rsplit("/", 1)[0]
                resource_pattern = f"{base}/{quote_key(directory)}/*"
        else:
            _validate_pattern(path_pattern)
            full_pattern = self._s3_key(path_pattern)
            if not self._pattern_matches(full_pattern, s3_key):
                raise AssetError(
                    f"path_pattern {path_pattern!r} does not cover key {key!r}"
                )
            resource_pattern = f"{base}/{quote_pattern(full_pattern)}"

        policy_b64, sig_b64, kp_id, _ = self._build_cf_custom_policy_params(
            resource_pattern, ttl
        )
        url = (
            f"{base}/{quote_key(s3_key)}"
            f"?Policy={policy_b64}&Signature={sig_b64}&Key-Pair-Id={kp_id}"
        )
        return AssetAccessUrl(url=url, expires_at=self._expires_at(ttl))

    def build_folder_signed_url(
        self,
        key: str,
        *,
        entry_filename: str,
        ttl_seconds: int | None = None,
    ) -> AssetAccessUrl:
        """Generate a CloudFront URL with **wildcard custom-policy** for the folder
        of *key*, pointing to *entry_filename* within that folder.

        This is the recommended method for composite assets such as HLS/DASH
        video streams.  The source asset (e.g. a transcoded ``.mp4``) and the
        player entry point (e.g. ``master.m3u8``) live in the same S3 folder.
        A single set of credentials authorises the manifest **and** all segment
        files that the player fetches from relative paths.

        Usage example::

            # key layout in S3:
            #   assets/<uuid>/<uuid>.mp4       ← original source
            #   assets/<uuid>/master.m3u8      ← HLS manifest
            #   assets/<uuid>/1080p/index.m3u8
            #   assets/<uuid>/1080p/seg000.ts  … seg009.ts

            url = repo.build_folder_signed_url(
                "assets/<uuid>/<uuid>.mp4",
                entry_filename="master.m3u8",
                ttl_seconds=7200,
            )
            # Returns:
            #   https://<cf>/assets/<uuid>/master.m3u8
            #       ?Policy=<wildcard over assets/<uuid>/*>
            #       &Signature=...&Key-Pair-Id=...

        Pass ``url.url`` directly to HLS.js as the source — the query-string
        credentials are inherited by all relative segment requests.

        Args:
            key:            Logical key of **any** file that belongs to the
                            target folder.  Used solely to derive the folder
                            path; the URL will **not** point to this file.
                            Example: ``"assets/<uuid>/<uuid>.mp4"``.
            entry_filename: Filename of the player entry point within the same
                            folder.  Example: ``"master.m3u8"``.
            ttl_seconds:    Lifetime of the signing credentials in seconds
                            (default: configured ``presign_ttl_seconds``).
                            A new URL with a new expiry is generated on every
                            call; credentials are not cached.

        Returns:
            :class:`AssetAccessUrl` whose ``url`` is
            ``https://<cf>/<folder>/<entry_filename>?Policy=…&Signature=…&Key-Pair-Id=…``
            and whose ``expires_at`` reflects the policy expiry.

        Raises:
            :exc:`AssetConfigurationError`: CloudFront signing is not configured
                (``cf_key_id`` or ``cf_private_key`` not set).
            :exc:`AssetError`: *key* has no directory component (it is a
                root-level key with no ``/`` separator).
        """
        if not self._cfg.has_cf_signing():
            raise AssetConfigurationError(
                "cf_key_id and cf_private_key must be set"
                " to use build_folder_signed_url"
            )
        _assert_no_leading_slash(key)
        s3_key = self._s3_key(key)

        if "/" not in s3_key:
            raise AssetError(
                f"Cannot derive folder from root-level key {key!r}; "
                "key must contain at least one '/' separator."
            )

        _validate_entry_filename(entry_filename)
        folder = s3_key.rsplit("/", 1)[0]  # e.g. "assets/<uuid>"
        base = self.cf_base_url()
        resource_pattern = f"{base}/{quote_key(folder)}/*"
        ttl = self._effective_ttl(ttl_seconds)

        policy_b64, sig_b64, kp_id, _ = self._build_cf_custom_policy_params(
            resource_pattern, ttl
        )
        entry_s3_key = f"{folder}/{entry_filename}"
        url = (
            f"{base}/{quote_key(entry_s3_key)}"
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
        _validate_pattern(key_pattern)
        ttl = self._effective_ttl(ttl_seconds)
        base = self.cf_base_url()
        s3_pattern = self._s3_key(key_pattern)
        resource_pattern = f"{base}/{quote_pattern(s3_pattern)}"

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
        *,
        content_length: int | None = None,
    ) -> UploadUrlResult:
        """Generate a presigned PUT URL for client-side upload.

        ``config.allowed_content_types`` is enforced here.  A presigned PUT
        cannot cap the body size by itself: pass *content_length* to bind the
        exact size into the signature (the client must then send exactly that
        many bytes; it is also checked against ``config.max_upload_bytes``).
        Without it, validate the stored object size after the upload.

        The client must send the file as an HTTP PUT with the ``Content-Type``
        header set to exactly the value provided here.  No other headers are
        required by default.

        Example (using ``requests``)::

            result = repo.build_upload_url("images/photo.jpg", "image/jpeg")
            with open("photo.jpg", "rb") as f:
                requests.put(result.url, data=f, headers=result.headers)
        """
        _assert_no_leading_slash(key)
        check_content_type(content_type, self._cfg.allowed_content_types)
        check_size(content_length, self._cfg.max_upload_bytes)
        ttl = self._effective_ttl(ttl_seconds)
        s3_key = self._s3_key(key)
        params: dict[str, Any] = {
            "Bucket": self._cfg.bucket,
            "Key": s3_key,
            "ContentType": content_type,
        }
        headers = {"Content-Type": content_type}
        if content_length is not None:
            params["ContentLength"] = content_length
            headers["Content-Length"] = str(content_length)
        try:
            # The legacy (SigV2) presigner does not sign Content-Length; SigV4
            # does, so a size-bound URL is generated with a SigV4 client.
            client = self._s3 if content_length is None else self._build_client("s3v4")
            url = client.generate_presigned_url(
                "put_object", Params=params, ExpiresIn=ttl
            )
        except Exception as exc:
            raise AssetError(
                f"Failed to generate presigned upload URL for {key!r}: {exc}"
            ) from exc
        return UploadUrlResult(
            url=url,
            method="PUT",
            headers=headers,
            expires_at=self._expires_at(ttl),
            key=key,
        )

    def resolve_access(
        self, key: str, ttl_seconds: int | None = None
    ) -> AssetAccessUrl:
        """Return public URL for public assets, signed download URL for private."""
        _assert_no_leading_slash(key)
        descriptor = self.get_descriptor(key)
        if descriptor.visibility == AssetVisibility.PUBLIC:
            return self.build_public_url(key)
        return self.build_download_url(key, ttl_seconds=ttl_seconds)


# Structural compatibility with the protocol is checked by mypy.
_: type[IAssetRepository] = S3AssetRepository
