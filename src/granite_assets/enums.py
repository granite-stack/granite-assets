"""Enumerations used across the granite-assets library."""

from enum import StrEnum


class AssetVisibility(StrEnum):
    """Controls how an asset is accessible.

    PUBLIC  – the asset is reachable via a stable, non-expiring URL (e.g. served
              by Nginx or as a public S3 object).
    PRIVATE – the asset requires a time-limited signed URL to be downloaded or
              uploaded; direct access is denied by the infrastructure.
    """

    PUBLIC = "public"
    PRIVATE = "private"


class CfSigningMethod(StrEnum):
    """Controls how CloudFront access is authorised for private assets.

    URL    – access credentials are embedded as query parameters on each URL
             (``?Policy=…&Signature=…&Key-Pair-Id=…`` for custom policy, or
             ``?Expires=…&Signature=…&Key-Pair-Id=…`` for canned policy).
             This is the default and works with any HTTP client without
             any cookie handling.

    COOKIE – access credentials are issued once as three ``Set-Cookie`` headers
             (``CloudFront-Policy``, ``CloudFront-Signature``,
             ``CloudFront-Key-Pair-Id``) via a dedicated endpoint.
             ``build_download_url`` returns a plain (unsigned) CloudFront URL;
             the browser includes the cookies automatically on every subsequent
             asset request.  Requires the LMS and the CloudFront distribution to
             share a domain (or use ``SameSite=None; Secure`` across distinct
             origins).  Use ``build_signed_cookies`` to obtain cookie values.
    """

    URL = "url"
    COOKIE = "cookie"
