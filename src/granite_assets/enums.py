"""Enumerations used across the granite-assets library."""

from enum import Enum


class AssetVisibility(str, Enum):
    """Controls how an asset is accessible.

    PUBLIC  – the asset is reachable via a stable, non-expiring URL (e.g. served
              by Nginx or as a public S3 object).
    PRIVATE – the asset requires a time-limited signed URL to be downloaded or
              uploaded; direct access is denied by the infrastructure.
    """

    PUBLIC = "public"
    PRIVATE = "private"
