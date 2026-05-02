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

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

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
    S3AssetRepositoryConfig,
    UploadUrlResult,
)

if TYPE_CHECKING:
    import boto3  # noqa: F401 – type-checking only
    from mypy_boto3_s3 import S3Client  # noqa: F401

_BACKEND_NAME = "S3AssetRepository"


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
        return datetime.now(tz=timezone.utc) + timedelta(seconds=ttl_seconds)

    def _public_url_for_key(self, s3_key: str) -> str:
        if self._cfg.public_base_url:
            base = self._cfg.public_base_url.rstrip("/")
            return f"{base}/{s3_key}"
        # Fall back to virtual-hosted-style URL
        bucket = self._cfg.bucket
        region = self._cfg.region
        return f"https://{bucket}.s3.{region}.amazonaws.com/{s3_key}"

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

        if request.metadata:
            put_kwargs["Metadata"] = request.metadata

        if request.checksum:
            put_kwargs["Metadata"] = {
                **(put_kwargs.get("Metadata") or {}),
                "x-checksum": request.checksum,
            }

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
        return AssetDescriptor(
            key=key,
            content_type=response.get("ContentType"),
            content_length=response.get("ContentLength"),
            last_modified=response.get("LastModified"),
            checksum=f"etag:{response.get('ETag', '').strip('\"')}",
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
        """Generate a presigned GET URL for the asset."""
        _assert_no_leading_slash(key)
        ttl = self._effective_ttl(ttl_seconds)
        s3_key = self._s3_key(key)
        try:
            url = self._s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._cfg.bucket, "Key": s3_key},
                ExpiresIn=ttl,
            )
        except Exception as exc:
            raise AssetError(
                f"Failed to generate presigned download URL for {key!r}: {exc}"
            ) from exc
        return AssetAccessUrl(url=url, expires_at=self._expires_at(ttl))

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
