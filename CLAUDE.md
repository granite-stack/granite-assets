# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`granite-assets` is a portable, framework-agnostic Python library (packaged for PyPI) that provides an
explicit asset-repository abstraction over storage backends — local filesystem served by Nginx, and AWS
S3 (or S3-compatible stores). It is not an ORM plugin; callers construct a config object and call the
repository directly. All repository methods are synchronous by design (callers wrap in
`asyncio.to_thread` / `run_in_executor` for async frameworks).

## Commands

```bash
make install         # uv sync + activates .githooks (core.hooksPath)
make lint             # ruff check src/ tests/
make format           # ruff format .
make type-check       # mypy src/ (strict mode)
make security-check   # bandit -r src/
make check            # lint + format + type-check + security-check
make test             # pytest -v  (includes integration tests if Docker is available)
make test-cov         # pytest with coverage (term + xml + html)
make docs             # sphinx-build docs_source -> docs/
make docs-serve       # build docs and serve at http://localhost:8000
```

Run a single test: `uv run pytest tests/test_s3.py::test_name -v`

Unit vs integration tests: integration tests are marked `@pytest.mark.integration` and spin up real
containers via `testcontainers` (Nginx, LocalStack S3, tusd) — they require Docker.

```bash
uv run pytest -m "not integration"   # unit tests only, no Docker required
uv run pytest -v                      # everything, including integration
```

CI (`.github/workflows/release.yml`) runs on every push to `main`: full test suite with
`--cov-fail-under=80`, then builds and publishes to PyPI and cuts a GitHub release using the version in
`pyproject.toml`. There is no separate PR-check workflow — pushing to `main` releases. Bump the version
first with `make bump-version PART=patch|minor|major` (syncs into `__init__.py` and
`docs_source/conf.py` via `scripts/update_version.py`).

### Pre-commit hook

`.githooks/pre-commit` (activated by `make install` via `git config core.hooksPath .githooks`) rebuilds
the Sphinx docs into `docs/` and `git add`s them on every commit, so the GitHub Pages docs never drift
from `docs_source/`. If you edit anything under `docs_source/`, expect `docs/` to be regenerated and
staged automatically at commit time — don't hand-edit `docs/`.

## Architecture

### Protocol-based backend abstraction

The core contract is `IAssetRepository` (`src/granite_assets/contracts.py`), a `@runtime_checkable`
`typing.Protocol`. Backends implement it via structural typing — no base class inheritance required, so
`isinstance(repo, IAssetRepository)` works on any conforming class. Two backends ship in
`src/granite_assets/repositories/`: `local_nginx.py` and `s3.py`. Adding a third backend means writing a
new module there that implements the same method set; see `docs_source/implementing-repository.rst` for
the checklist.

Method groups on the protocol:
- Write: `save`, `delete`, `copy`, `move`
- Query: `exists`, `get_descriptor`
- URL construction (pure, no network I/O): `build_public_url`, `build_download_url`, `build_upload_url`,
  `resolve_access`

### Config objects drive everything, no globals

Each backend has its own frozen-ish `@dataclass(slots=True)` config in `models.py`
(`LocalNginxAssetRepositoryConfig`, `S3AssetRepositoryConfig`). There is no global settings object or
singleton — every repository is constructed by passing its config directly. `factory.py` provides
`build_asset_repository(config)`, a `@overload`-typed dispatcher that picks the concrete repository class
by config type, so callers who select a backend at runtime don't need `if/else` on backend names and
still get a narrowed static return type at call sites where the config type is known.

### Optional extras via lazy imports, not hard dependencies

Only `boto3` is a hard dependency. `S3AssetRepository` (`repositories/s3.py`) imports `boto3` lazily
inside the class so `import granite_assets` never fails without the S3 extra installed; the top-level
`__init__.py` wraps the `S3AssetRepository` import in `try/except ImportError` and only adds it to
`__all__` on success. The same pattern applies to `granite_assets.migrations.upgrade_to_head`, gated
behind the `db` extra (`sqlalchemy` + `alembic`) — always check for this lazy-import + `try/except`
pattern before assuming a symbol is unconditionally available.

### Visibility model

`AssetVisibility` (`PUBLIC`/`PRIVATE`) is the single axis controlling URL behavior across backends — see
the visibility table in `README.md` for the full support matrix per backend/method. Public assets get
permanent, non-expiring URLs; private assets require backend-specific signed access
(`build_download_url`/`build_upload_url` raise `AssetAccessNotSupportedError` when unsupported, e.g. any
upload URL on the local backend, or download URLs on local when `secure_link_secret` isn't configured).

### Local backend: Nginx does serving, this library does filesystem + signing

`LocalNginxAssetRepository` never serves HTTP itself — it only reads/writes files under
`storage_path/{public,private}_prefix/`. Access control for private files is delegated to Nginx
(`internal`, `auth_request`, or `secure_link`). When `secure_link_secret` is set, signed URLs are
generated using a token algorithm that must exactly match Nginx's `ngx_http_secure_link_module`
(`md5(f"{expires}{uri} {secret}")`, base64url, `=` stripped) — see the module docstring in
`repositories/local_nginx.py` for the precise format before touching that code. Uploads can go through
one of three paths controlled by config: direct app-layer `save()`, a `tusd` resumable-upload server
(`tusd_url`), or a simple HMAC-signed PUT upload service (`upload_service_url`) — these are mutually
exclusive fallbacks checked in that order.

### S3 backend: presigned PUT (not POST), lazy boto3, optional CloudFront

`S3AssetRepository` uses presigned PUT rather than presigned POST for uploads (simpler for
fetch/axios/curl/mobile clients — see module docstring for the tradeoff). Visibility is expressed via
object ACL (`public-read` vs none) unless `use_object_acl=False`, needed for buckets with Object
Ownership = `BucketOwnerEnforced`. CloudFront support is optional and layered on top: setting
`cf_key_id`/`cf_private_key` switches `build_download_url` to CloudFront signed URLs (RSA, via
`CfSigningMethod.URL` or `.COOKIE`); setting `cf_unsigned_urls=True` + `public_base_url` (without signing
keys) returns permanent unsigned CloudFront URLs instead. `has_cf_signing()` takes precedence over
`has_cf_unsigned_url()` when both are configured — check `models.py` for the exact precedence logic
before changing URL-resolution behavior.

### Exceptions

All library exceptions derive from `AssetError` (`exceptions.py`): `AssetNotFoundError` (missing key),
`AssetAccessNotSupportedError` (backend/visibility doesn't support the requested URL type — carries
`backend`/`operation`), `AssetConfigurationError` (bad config). Keep new failure modes inside this
hierarchy rather than letting backend-native exceptions (`botocore.exceptions.*`, `OSError`) leak through
the public API.

### Migrations are a separate, optional subsystem

`migrations.py` + `src/granite_assets/alembic/` exist so host applications can call
`upgrade_to_head(database_url)` to apply granite-assets' own schema migrations (currently a no-op stamp,
as there are no DB models yet) alongside their own Alembic setup, using a dedicated
`granite_assets_alembic_version` table so it never collides with the host app's `alembic_version` table.
This only matters if you're adding DB-backed models to the library.
