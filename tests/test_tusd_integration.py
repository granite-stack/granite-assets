"""Integration tests for LocalNginxAssetRepository.build_upload_url with tusd.

These tests spin up a real tusd container using testcontainers and verify the
complete tus upload flow:

1. ``build_upload_url`` generates a valid tus creation URL pointing at tusd.
2. A POST to that URL creates an upload resource (tus creation request).
3. A PATCH to the returned ``Location`` uploads the file data.
4. The uploaded file lands in tusd's data directory on disk.
5. tusd writes an ``.info`` sidecar with our Upload-Metadata fields.

No pre-create / post-finish hooks are configured in this test environment —
tusd accepts all uploads unconditionally.  The ``upload-token`` in
``Upload-Metadata`` is still generated and embedded so that unit tests can
verify its HMAC; hook validation is the application layer's responsibility and
is covered in ``test_local_nginx.py``.

Prerequisites
-------------
* Docker must be running on the host executing these tests.
* No manual ``docker compose`` setup is required.

Marks
-----
All tests are marked ``pytest.mark.integration`` so they can be skipped from
fast unit-test runs::

    # Unit tests only (skip Docker):
    pytest -m "not integration"

    # Everything including integration tests:
    pytest
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from granite_assets.enums import AssetVisibility
from granite_assets.models import LocalNginxAssetRepositoryConfig, UploadUrlResult
from granite_assets.repositories.local_nginx import LocalNginxAssetRepository

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UPLOAD_SECRET = "test-upload-secret"
_TUSD_IMAGE = "tusproject/tusd:latest"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def tusd_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Shared temp directory mounted into the tusd container as ``/data``.

    World-writable (0o777) so the non-root tusd process can create files.
    """
    root = tmp_path_factory.mktemp("tusd-data")
    root.chmod(0o777)
    return root


@pytest.fixture(scope="session")
def tusd_container(tusd_data_dir: Path):  # type: ignore[return]
    """Start a tusd container and yield its base URL (e.g. ``http://localhost:PORT``).

    The container is shared across the whole test session to avoid the overhead
    of starting a new container for every test.
    """
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    container = (
        DockerContainer(_TUSD_IMAGE)
        # Override default upload directory to the host-mounted path.
        .with_command(["-upload-dir", "/data"])
        .with_volume_mapping(str(tusd_data_dir), "/data", "rw")
        .with_exposed_ports(8080)
        # tusd logs this line when it is ready to accept connections.
        .waiting_for(LogMessageWaitStrategy("You can now upload files to:"))
    )

    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(8080)
        yield f"http://{host}:{port}"


@pytest.fixture()
def repo(tusd_container: str, tmp_path: Path) -> LocalNginxAssetRepository:
    """Repository instance configured to point at the running tusd container."""
    cfg = LocalNginxAssetRepositoryConfig(
        storage_path=str(tmp_path / "assets"),
        base_url="http://localhost:8080/assets",  # not exercised in upload tests
        tusd_url=tusd_container,
        upload_secret=_UPLOAD_SECRET,
        upload_ttl_seconds=3600,
        create_directories=True,
    )
    return LocalNginxAssetRepository(cfg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tus_upload(
    create_url: str,
    headers: dict[str, str],
    content: bytes,
    *,
    timeout: float = 10.0,
) -> tuple[str, int]:
    """Perform a complete tus upload (create + PATCH).

    Args:
        create_url: URL for the tus creation POST request.
        headers:    Headers from ``UploadUrlResult.headers`` (already includes
                    ``Tus-Resumable`` and ``Upload-Metadata``).
        content:    Raw file bytes to upload.
        timeout:    HTTP request timeout in seconds.

    Returns:
        ``(upload_location, final_offset)`` — the canonical URL of the upload
        resource and the ``Upload-Offset`` value returned by the final PATCH.
    """
    # Step 1 — create the upload resource (tus creation request).
    # We must add Upload-Length so tusd knows the total file size.
    create_headers = {
        **headers,
        "Upload-Length": str(len(content)),
    }
    resp = httpx.post(create_url, headers=create_headers, timeout=timeout)
    assert resp.status_code == 201, (
        f"tus creation failed with {resp.status_code}: {resp.text!r}"
    )
    location = resp.headers["Location"]

    # Step 2 — upload the file data as a single PATCH.
    patch_headers = {
        "Tus-Resumable": "1.0.0",
        "Content-Type": "application/offset+octet-stream",
        "Upload-Offset": "0",
        "Content-Length": str(len(content)),
    }
    resp2 = httpx.patch(location, headers=patch_headers, content=content, timeout=timeout)
    assert resp2.status_code == 204, (
        f"tus PATCH failed with {resp2.status_code}: {resp2.text!r}"
    )
    final_offset = int(resp2.headers.get("Upload-Offset", "-1"))
    return location, final_offset


def _parse_metadata(upload_metadata_header: str) -> dict[str, str]:
    """Parse a tus Upload-Metadata header into a ``{key: decoded_value}`` dict."""
    result: dict[str, str] = {}
    for entry in upload_metadata_header.split(","):
        parts = entry.strip().split(" ", 1)
        if len(parts) == 2:
            result[parts[0]] = base64.b64decode(parts[1]).decode()
    return result


def _upload_id_from_location(location: str) -> str:
    """Extract the tusd upload ID from a Location header value."""
    return location.rstrip("/").rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# Tests — URL generation
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_build_upload_url_points_at_tusd(
    repo: LocalNginxAssetRepository,
    tusd_container: str,
) -> None:
    """``build_upload_url`` returns a ``UploadUrlResult`` targeting the tusd server."""
    result = repo.build_upload_url("docs/report.pdf", "application/pdf")

    assert isinstance(result, UploadUrlResult)
    assert result.method == "POST"
    assert result.url == f"{tusd_container}/files/"
    assert result.key == "docs/report.pdf"
    assert result.expires_at is not None


@pytest.mark.integration
def test_build_upload_url_has_required_tus_headers(
    repo: LocalNginxAssetRepository,
) -> None:
    """The result headers include all fields required by the tus creation request."""
    result = repo.build_upload_url("docs/spec.pdf", "application/pdf")

    assert result.headers["Tus-Resumable"] == "1.0.0"
    assert "Upload-Metadata" in result.headers
    assert result.headers["Content-Length"] == "0"


@pytest.mark.integration
def test_build_upload_url_metadata_fields(
    repo: LocalNginxAssetRepository,
) -> None:
    """Upload-Metadata contains all fields the hook needs to route the file."""
    result = repo.build_upload_url(
        "images/avatar.jpg",
        "image/jpeg",
        visibility=AssetVisibility.PUBLIC,
    )
    meta = _parse_metadata(result.headers["Upload-Metadata"])

    assert meta["asset-key"] == "images/avatar.jpg"
    assert meta["content-type"] == "image/jpeg"
    assert meta["visibility"] == "public"
    assert meta["upload-expires"].isdigit()
    assert len(meta["upload-token"]) == 64  # SHA-256 hex digest


# ---------------------------------------------------------------------------
# Tests — tus protocol (live requests to tusd)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_tus_create_returns_201(repo: LocalNginxAssetRepository) -> None:
    """A tus creation POST with Upload-Length returns 201 Created with a Location."""
    content = b"hello from granite-assets integration tests"
    result = repo.build_upload_url("uploads/hello.txt", "text/plain")

    headers = {**result.headers, "Upload-Length": str(len(content))}
    resp = httpx.post(result.url, headers=headers, timeout=10)

    assert resp.status_code == 201
    assert "Location" in resp.headers
    assert resp.headers["Tus-Resumable"] == "1.0.0"


@pytest.mark.integration
def test_tus_upload_complete_offset_matches_size(
    repo: LocalNginxAssetRepository,
) -> None:
    """After a successful PATCH the Upload-Offset equals the file size."""
    content = b"complete upload integration test data"
    result = repo.build_upload_url("uploads/complete.bin", "application/octet-stream")

    _, final_offset = _tus_upload(result.url, result.headers, content)

    assert final_offset == len(content)


@pytest.mark.integration
def test_tus_upload_file_lands_on_disk(
    repo: LocalNginxAssetRepository,
    tusd_data_dir: Path,
) -> None:
    """The uploaded bytes are written verbatim to the tusd data directory."""
    content = b"file that must land on disk"
    result = repo.build_upload_url("uploads/disk-check.bin", "application/octet-stream")

    location, final_offset = _tus_upload(result.url, result.headers, content)

    assert final_offset == len(content)

    # tusd writes a raw data file named after the upload ID.
    upload_id = _upload_id_from_location(location)
    data_file = tusd_data_dir / upload_id
    assert data_file.exists(), f"Upload data file not found at {data_file}"
    assert data_file.read_bytes() == content


@pytest.mark.integration
def test_tus_info_sidecar_contains_our_metadata(
    repo: LocalNginxAssetRepository,
    tusd_data_dir: Path,
) -> None:
    """tusd writes a ``{id}.info`` sidecar with our Upload-Metadata decoded."""
    content = b"metadata sidecar check"
    result = repo.build_upload_url("uploads/sidecar-check.txt", "text/plain")

    location, _ = _tus_upload(result.url, result.headers, content)

    upload_id = _upload_id_from_location(location)
    info_path = tusd_data_dir / f"{upload_id}.info"
    assert info_path.exists(), f".info sidecar not found at {info_path}"

    info: dict[str, Any] = json.loads(info_path.read_text())
    stored_meta: dict[str, str] = info.get("MetaData", {})

    # tusd stores the decoded (plain-text) values.
    assert stored_meta.get("asset-key") == "uploads/sidecar-check.txt"
    assert stored_meta.get("content-type") == "text/plain"


@pytest.mark.integration
def test_tus_upload_public_visibility(repo: LocalNginxAssetRepository) -> None:
    """build_upload_url with visibility=PUBLIC sends 'public' in metadata and uploads."""
    content = b"public asset upload test"
    result = repo.build_upload_url(
        "public/logo.png",
        "image/png",
        visibility=AssetVisibility.PUBLIC,
    )
    meta = _parse_metadata(result.headers["Upload-Metadata"])
    assert meta["visibility"] == "public"

    _, final_offset = _tus_upload(result.url, result.headers, content)
    assert final_offset == len(content)


@pytest.mark.integration
def test_tus_upload_ttl_override_reflected_in_metadata(
    repo: LocalNginxAssetRepository,
) -> None:
    """ttl_seconds override is reflected in the ``upload-expires`` metadata field."""
    before = int(time.time())
    result = repo.build_upload_url(
        "uploads/ttl-test.bin", "application/octet-stream", ttl_seconds=7200
    )
    after = int(time.time())

    meta = _parse_metadata(result.headers["Upload-Metadata"])
    expires = int(meta["upload-expires"])
    assert before + 7200 <= expires <= after + 7200

    # The URL is still usable within the TTL window.
    content = b"ttl override test"
    _, final_offset = _tus_upload(result.url, result.headers, content)
    assert final_offset == len(content)
