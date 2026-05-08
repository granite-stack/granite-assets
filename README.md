# granite-assets

[![PyPI](https://img.shields.io/pypi/v/granite-assets.svg)](https://pypi.org/project/granite-assets/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Typed](https://img.shields.io/badge/typing-py.typed-informational)](src/granite_assets/py.typed)


A portable, framework-agnostic Python library for managing public and private assets across different storage backends — local filesystem (Nginx-served) and AWS S3, with support for custom backends via a clean protocol interface.

`granite-assets` is **not** an ORM storage plugin or a media-field handler. It is an explicit *asset repository* layer that your application code calls directly. You pass configuration objects to the repository constructor — no global settings, no hidden singletons.

---

## Features

- Unified `IAssetRepository` protocol for all backends (`@runtime_checkable`)
- Save, delete, copy, move, and check existence of assets
- Build permanent public URLs (CDN or native endpoint)
- Generate presigned download URLs for private assets (S3)
- Generate presigned upload URLs for direct client-to-storage uploads (S3)
- Lightweight asset metadata queries via `get_descriptor()` — no download needed
- Strong typing with `@dataclass(slots=True)` models and full type hints
- `py.typed` marker — works with mypy strict mode and pyright
- Designed to integrate with FastAPI, Django, Celery, or any Python framework
- All methods are **synchronous** — use `asyncio.to_thread` / `run_in_executor` in async contexts

---

## Requirements

- Python 3.12+
- `boto3 >= 1.34`

## Installation

```bash
pip install granite-assets
```

or with [uv](https://docs.astral.sh/uv/):

```bash
uv add granite-assets
```

### Development

```bash
git clone https://github.com/granite-stack/granite-assets.git
cd granite-assets
make install
```

`make install` installs all dependencies **and** configures the local git hook path (see [Pre-commit hook](#pre-commit-hook) below).

### Pre-commit hook

This project ships a pre-commit hook at `.githooks/pre-commit` that rebuilds the Sphinx documentation and stages any changed files under `docs/` before every commit. This keeps the GitHub Pages documentation in sync without requiring a separate CI step.

`make install` activates it automatically. To manage it manually:

```bash
# Activate
git config core.hooksPath .githooks

# Deactivate (restore default git hooks directory)
git config --unset core.hooksPath
```

### Documentation

```bash
uv sync --group docs
make docs        # builds to docs/
make docs-serve  # serves on http://localhost:8000
```

---

## Quick Start

### Local filesystem (Nginx) backend

```python
from granite_assets import (
    LocalNginxAssetRepositoryConfig,
    LocalNginxAssetRepository,
    AssetSaveRequest,
    AssetVisibility,
)

config = LocalNginxAssetRepositoryConfig(
    storage_path="/var/www/assets",
    base_url="https://static.example.com/assets",
    public_prefix="public",
    private_prefix="private",
    create_directories=True,
)

repo = LocalNginxAssetRepository(config)

# Save a public file
with open("logo.png", "rb") as fh:
    result = repo.save(AssetSaveRequest(
        key="brand/logo.png",
        source=fh,
        content_type="image/png",
        visibility=AssetVisibility.PUBLIC,
    ))

print(result.backend_ref)   # /var/www/assets/public/brand/logo.png
print(result.checksum)      # md5:abc123...

# Permanent public URL
url = repo.build_public_url("brand/logo.png")
print(url.url)          # https://static.example.com/assets/public/brand/logo.png
print(url.is_permanent) # True

# Metadata without downloading
desc = repo.get_descriptor("brand/logo.png")
print(desc.content_length, desc.last_modified)

# Copy, move, delete
repo.copy("brand/logo.png", "brand/logo-backup.png")
repo.move("brand/logo-backup.png", "archive/logo.png")
repo.delete("archive/logo.png")
```

### AWS S3 backend

```python
from granite_assets import (
    S3AssetRepositoryConfig,
    AssetSaveRequest,
    AssetVisibility,
    build_asset_repository,
)

config = S3AssetRepositoryConfig(
    bucket="my-assets-bucket",
    region="eu-west-1",
    public_base_url="https://cdn.example.com",  # optional CDN prefix
    key_prefix="production/",                    # optional key namespace
    presign_ttl_seconds=3600,
)

repo = build_asset_repository(config)  # → S3AssetRepository

# Save a private asset
with open("invoice.pdf", "rb") as fh:
    result = repo.save(AssetSaveRequest(
        key="invoices/2024/inv-001.pdf",
        source=fh,
        content_type="application/pdf",
        visibility=AssetVisibility.PRIVATE,
    ))

# Presigned download URL (expires in 1 hour by default)
download = repo.build_download_url("invoices/2024/inv-001.pdf")
print(download.url)          # https://my-assets-bucket.s3.eu-west-1.amazonaws.com/...?X-Amz-Signature=...
print(download.expires_at)   # datetime(...)
print(download.is_permanent) # False

# Presigned upload URL for direct browser/client upload
upload = repo.build_upload_url(
    "avatars/user-123.jpg",
    content_type="image/jpeg",
    ttl_seconds=900,
)
print(upload.url)           # https://...amazonaws.com/...?X-Amz-...
print(upload.method)        # PUT
print(upload.headers)       # {"Content-Type": "image/jpeg"}

# The client then performs:
# PUT <upload.url> with Content-Type: image/jpeg in headers
```

### Using the factory

When the backend is selected at runtime (e.g. from a settings object or environment variable), use `build_asset_repository` instead of importing the concrete class:

```python
from granite_assets import build_asset_repository, LocalNginxAssetRepositoryConfig

config = LocalNginxAssetRepositoryConfig(
    storage_path="/var/www/assets",
    base_url="http://localhost/assets",
)
repo = build_asset_repository(config)  # → LocalNginxAssetRepository
```

---

## Asset visibility

| Visibility | Meaning | `build_public_url` | `build_download_url` | `build_upload_url` |
|---|---|---|---|---|
| `PUBLIC` | Accessible without authentication | ✅ Permanent URL | ✅ (same permanent URL) | S3 only |
| `PRIVATE` | Requires a signed URL | ❌ | ✅ Signed, expiring URL | S3 only |

### Public vs signed URLs

**Public URL** — a stable, non-expiring URL that anyone with the link can access. Served by Nginx (local backend) or by S3 with `public-read` ACL (or a CloudFront distribution). Use for product images, static assets, and public documents.

**Signed download URL** — a time-limited URL generated by S3 (presigned GET). It encodes credentials in the query string and expires after the configured TTL. Use for invoices, reports, user uploads, or any asset that requires access control.

**Signed upload URL** — a time-limited presigned PUT URL that allows a client (browser, mobile app) to upload directly to S3 without routing the file body through your application server. After the upload completes, call `repo.exists()` to verify or consume an S3 event notification.

---

## Backend comparison

| Feature | `LocalNginxAssetRepository` | `S3AssetRepository` |
|---|---|---|
| `save` | ✅ | ✅ |
| `delete` | ✅ | ✅ |
| `copy` | ✅ (shutil) | ✅ (server-side) |
| `move` | ✅ (shutil) | ✅ (copy + delete) |
| `exists` | ✅ | ✅ |
| `get_descriptor` | ✅ | ✅ |
| `build_public_url` | ✅ public assets only | ✅ |
| `build_download_url` | ✅ public / ❌ private | ✅ presigned GET |
| `build_upload_url` | ❌ | ✅ presigned PUT |

---

## Local backend limitations

`LocalNginxAssetRepository` has intentional design limitations:

1. **No presigned URLs.** The local filesystem has no mechanism to generate time-limited, signed access tokens. `build_download_url` raises `AssetAccessNotSupportedError` for private assets. Route private asset downloads through your application (validate the session, then stream the file).

2. **No client-side upload URLs.** `build_upload_url` always raises `AssetAccessNotSupportedError`. Uploads must go through your application layer, which then calls `repo.save(...)`.

3. **HTTP access control is your responsibility.** The library places private assets under the `private_prefix` directory, but only Nginx configuration (`auth_request`, `internal`, etc.) can enforce actual HTTP-level access control.

Example Nginx configuration:

```nginx
location /assets/public/ {
    alias /var/www/assets/public/;
}

# Private assets: only accessible via X-Accel-Redirect from your app
location /assets/private/ {
    internal;
    alias /var/www/assets/private/;
}
```

---

---

## Configuration reference

### `LocalNginxAssetRepositoryConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `storage_path` | `str` | required | Absolute path on disk where assets are written |
| `base_url` | `str` | required | Root URL at which Nginx serves `storage_path` |
| `public_prefix` | `str` | `"public"` | Sub-path for public assets |
| `private_prefix` | `str` | `"private"` | Sub-path for private assets |
| `overwrite` | `bool` | `True` | Allow overwriting existing files |
| `create_directories` | `bool` | `True` | Auto-create missing parent directories |

### `S3AssetRepositoryConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `bucket` | `str` | required | S3 bucket name |
| `region` | `str` | required | AWS region |
| `public_base_url` | `str \| None` | `None` | CDN or custom domain for public asset URLs |
| `key_prefix` | `str` | `""` | Prefix prepended to all S3 keys |
| `presign_ttl_seconds` | `int` | `3600` | Default TTL for presigned URLs |
| `endpoint_url` | `str \| None` | `None` | Custom endpoint for S3-compatible stores (MinIO, etc.) |
| `access_key_id` | `str \| None` | `None` | Explicit AWS credentials (falls back to boto3 chain) |
| `secret_access_key` | `str \| None` | `None` | Explicit AWS credentials |
| `session_token` | `str \| None` | `None` | STS session token |

---

## Error handling

All exceptions derive from `AssetError`:

```python
from granite_assets import (
    AssetError,
    AssetNotFoundError,
    AssetAccessNotSupportedError,
    AssetConfigurationError,
)

try:
    repo.delete("missing/key.jpg")
except AssetNotFoundError as e:
    print(f"Not found: {e}")

try:
    repo.build_upload_url("key.jpg", "image/jpeg")  # on LocalNginx
except AssetAccessNotSupportedError as e:
    print(f"Unsupported: {e}")

try:
    repo.save(request)
except AssetError as e:
    # Base class — catches all granite-assets errors
    print(e)
```

---

## FastAPI integration

Since all repository methods are synchronous, wrap them in `asyncio.to_thread` inside async endpoints to avoid blocking the event loop:

```python
import asyncio
from fastapi import FastAPI, UploadFile, Depends
from granite_assets import (
    S3AssetRepository,
    S3AssetRepositoryConfig,
    AssetSaveRequest,
    AssetVisibility,
)

def get_repo() -> S3AssetRepository:
    return S3AssetRepository(S3AssetRepositoryConfig(
        bucket="my-bucket",
        region="eu-west-1",
    ))

app = FastAPI()

@app.post("/upload")
async def upload(
    file: UploadFile,
    repo: S3AssetRepository = Depends(get_repo),
):
    content = await file.read()
    result = await asyncio.to_thread(
        repo.save,
        AssetSaveRequest(
            key=f"uploads/{file.filename}",
            source=content,
            content_type=file.content_type or "application/octet-stream",
            visibility=AssetVisibility.PRIVATE,
            filename=file.filename,
        ),
    )
    return {"key": result.key, "size": result.content_length}

@app.get("/download-url/{key:path}")
async def get_download_url(key: str, repo: S3AssetRepository = Depends(get_repo)):
    url = await asyncio.to_thread(repo.build_download_url, key, 300)
    return {"url": url.url, "expires_at": url.expires_at}
```

---

## Implementing a custom backend

Because `IAssetRepository` is a `@runtime_checkable` `Protocol`, you do not need to inherit from any base class. Implement the required methods and the library will accept your class:

```python
from granite_assets import IAssetRepository, AssetSaveRequest, AssetSaveResult

class MyCustomRepository:
    def save(self, request: AssetSaveRequest) -> AssetSaveResult: ...
    def delete(self, key: str) -> None: ...
    def copy(self, source_key: str, dest_key: str, *, overwrite: bool = True) -> None: ...
    def move(self, source_key: str, dest_key: str, *, overwrite: bool = True) -> None: ...
    def exists(self, key: str) -> bool: ...
    def get_descriptor(self, key: str): ...
    def build_public_url(self, key: str): ...
    def build_download_url(self, key: str, ttl_seconds=None): ...
    def build_upload_url(self, key: str, content_type: str, ttl_seconds=None): ...

assert isinstance(MyCustomRepository(), IAssetRepository)  # True
```

See the [full guide](docs_source/implementing-repository.rst) for a complete Azure Blob Storage example and an implementation checklist.

---

## Development commands

```bash
make lint           # ruff check
make format         # ruff format
make type-check     # mypy
make security-check # bandit
make check          # all of the above
make test           # pytest
make test-cov       # pytest + coverage
make docs           # build Sphinx docs → docs/
make docs-serve     # build + serve on http://localhost:8000
```

---

## License

[MIT](LICENSE)
