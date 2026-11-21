"""Key and upload-policy helpers shared by every repository backend."""

from __future__ import annotations

import os
import re
import uuid as _uuid_mod
from collections.abc import Iterable
from pathlib import PureWindowsPath
from urllib.parse import quote

from granite_assets.exceptions import AssetError

_EXT_RE = re.compile(r"^\.[a-z0-9]{1,10}$")


def validate_key(key: str) -> None:
    """Reject unsafe asset keys.

    A valid key is a non-empty relative POSIX path without control characters,
    backslashes, drive letters, empty segments or ``..`` segments.
    """
    if not key:
        raise AssetError("Asset key must not be empty")
    if "\x00" in key:
        raise AssetError(f"Asset key must not contain NUL bytes: {key!r}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in key):
        raise AssetError(f"Asset key must not contain control characters: {key!r}")
    if key.startswith("/"):
        raise AssetError(f"Asset key must not start with '/': {key!r}")
    if "\\" in key:
        raise AssetError(f"Asset key must not contain backslashes: {key!r}")
    if PureWindowsPath(key).drive:
        raise AssetError(f"Asset key must not be an absolute path: {key!r}")
    segments = key.split("/")
    if ".." in segments:
        raise AssetError(f"Asset key must not contain '..' segments: {key!r}")
    if "" in segments:
        raise AssetError(f"Asset key must not contain empty segments: {key!r}")


def resolve_asset_key(key: str | None, filename: str | None) -> str:
    """Return the final storage key.

    * *key* is ``None``  ->  auto-generate ``<uuid>/<uuid>.<ext>``.
    * *key* has no file extension  ->  treat it as a folder prefix and append
      ``/<last_segment><ext>``.
    * *key* has a file extension  ->  use it unchanged.

    The extension taken from *filename* is only used when it is a short
    alphanumeric suffix (``[a-z0-9]{1,10}``); anything else is ignored.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if not _EXT_RE.match(ext):
        ext = ""
    if key is None:
        asset_id = str(_uuid_mod.uuid4())
        return f"{asset_id}/{asset_id}{ext}"
    _, key_ext = os.path.splitext(key)
    if not key_ext:
        folder = key.rstrip("/")
        last_segment = folder.rsplit("/", 1)[-1]
        return f"{folder}/{last_segment}{ext}"
    return key


def quote_key(key: str) -> str:
    """Percent-encode a key for use in a URL path (``/`` is preserved)."""
    return quote(key, safe="/")


def check_content_type(content_type: str, allowed: Iterable[str] | None) -> None:
    """Raise :class:`AssetError` if *content_type* is not in *allowed*.

    ``allowed`` entries are compared case-insensitively, ignoring parameters
    such as ``; charset=utf-8``.  Entries ending in ``/*`` match a whole
    top-level type (``image/*``).  ``None`` disables the check.
    """
    if allowed is None:
        return
    base = content_type.split(";", 1)[0].strip().lower()
    for entry in allowed:
        pattern = entry.strip().lower()
        if pattern == base or (
            pattern.endswith("/*") and base.startswith(pattern[:-1])
        ):
            return
    raise AssetError(f"Content type not allowed: {content_type!r}")


def check_size(size: int | None, max_bytes: int | None) -> None:
    """Raise :class:`AssetError` if *size* exceeds *max_bytes*."""
    if max_bytes is not None and size is not None and size > max_bytes:
        raise AssetError(f"Asset exceeds the maximum upload size of {max_bytes} bytes")
