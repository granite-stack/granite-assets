"""Regression tests for the local backend: visibility, atomicity, limits, URLs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from granite_assets._keys import check_content_type, resolve_asset_key, validate_key
from granite_assets.enums import AssetVisibility
from granite_assets.exceptions import AssetError, AssetNotFoundError
from granite_assets.models import AssetSaveRequest, LocalNginxAssetRepositoryConfig
from granite_assets.repositories.local_nginx import LocalNginxAssetRepository

PUB = AssetVisibility.PUBLIC
PRIV = AssetVisibility.PRIVATE


def make_repo(tmp_path: Path, **kw: object) -> LocalNginxAssetRepository:
    cfg = LocalNginxAssetRepositoryConfig(
        storage_path=str(tmp_path / "assets"),
        base_url="http://h/assets",
        secure_link_secret="s3cret",
        **kw,  # type: ignore[arg-type]
    )
    return LocalNginxAssetRepository(cfg)


def save(
    repo: LocalNginxAssetRepository,
    key: str,
    data: bytes,
    vis: AssetVisibility = PRIV,
    **kw: object,
) -> None:
    repo.save(
        AssetSaveRequest(
            source=data,
            content_type="text/plain",
            key=key,
            visibility=vis,
            **kw,  # type: ignore[arg-type]
        )
    )


def on_disk(tmp_path: Path, vis: str, key: str) -> Path:
    return tmp_path / "assets" / vis / key


# ---- GA-002 / GA-004: one key, one visibility -------------------------------


def test_resave_as_private_removes_public_copy(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    save(repo, "k/f.txt", b"old", PUB)
    save(repo, "k/f.txt", b"new", PRIV)
    assert not on_disk(tmp_path, "public", "k/f.txt").exists()
    assert on_disk(tmp_path, "private", "k/f.txt").read_bytes() == b"new"
    assert "/private/" in repo.build_download_url("k/f.txt").url


def test_resave_as_public_removes_private_copy(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    save(repo, "k/f.txt", b"old", PRIV)
    save(repo, "k/f.txt", b"new", PUB)
    assert not on_disk(tmp_path, "private", "k/f.txt").exists()


def test_visibility_change_refused_without_overwrite(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    save(repo, "k/f.txt", b"old", PUB)
    with pytest.raises(AssetError):
        save(repo, "k/f.txt", b"new", PRIV, overwrite=False)
    assert on_disk(tmp_path, "public", "k/f.txt").read_bytes() == b"old"
    assert not on_disk(tmp_path, "private", "k/f.txt").exists()


def test_delete_removes_every_copy(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    # Legacy state with both copies present.
    for vis in ("public", "private"):
        p = on_disk(tmp_path, vis, "k/f.txt")
        p.parent.mkdir(parents=True)
        p.write_bytes(b"x")
    repo.delete("k/f.txt")
    assert not repo.exists("k/f.txt")
    with pytest.raises(AssetNotFoundError):
        repo.delete("k/f.txt")


def test_copy_and_move_remove_stale_other_visibility(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    save(repo, "src.txt", b"a", PRIV)
    save(repo, "dst.txt", b"b", PUB)
    repo.copy("src.txt", "dst.txt")
    assert not on_disk(tmp_path, "public", "dst.txt").exists()
    save(repo, "dst2.txt", b"b", PUB)
    repo.move("src.txt", "dst2.txt")
    assert not on_disk(tmp_path, "public", "dst2.txt").exists()
    assert on_disk(tmp_path, "private", "dst2.txt").read_bytes() == b"a"


def test_copy_refuses_when_other_visibility_exists(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    save(repo, "src.txt", b"a", PRIV)
    save(repo, "dst.txt", b"b", PUB)
    with pytest.raises(AssetError):
        repo.copy("src.txt", "dst.txt", overwrite=False)


# ---- GA-015: native exceptions / same file -----------------------------------


def test_copy_and_move_onto_itself_are_noops(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    save(repo, "a.txt", b"a")
    repo.copy("a.txt", "a.txt")
    repo.move("a.txt", "a.txt")
    assert on_disk(tmp_path, "private", "a.txt").read_bytes() == b"a"


def test_copy_without_parent_dir_raises_asset_error(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, create_directories=False)
    (tmp_path / "assets" / "private").mkdir(parents=True)
    on_disk(tmp_path, "private", "a.txt").write_bytes(b"a")
    with pytest.raises(AssetError):
        repo.copy("a.txt", "nodir/b.txt")
    with pytest.raises(AssetError):
        repo.move("a.txt", "nodir/b.txt")


# ---- GA-007: atomic write ----------------------------------------------------


class _Boom(io.RawIOBase):
    def __init__(self) -> None:
        self.sent = False

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if not self.sent:
            self.sent = True
            return b"partial"
        raise OSError("stream broke")


def test_failed_write_keeps_previous_file_and_leaves_no_temp(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    save(repo, "k/f.txt", b"good")
    with pytest.raises(AssetError):
        repo.save(
            AssetSaveRequest(
                source=_Boom(),  # type: ignore[arg-type]
                content_type="text/plain",
                key="k/f.txt",
            )
        )
    assert on_disk(tmp_path, "private", "k/f.txt").read_bytes() == b"good"
    assert [p.name for p in on_disk(tmp_path, "private", "k").iterdir()] == ["f.txt"]


def test_concurrent_saves_without_overwrite_only_one_wins(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    results: list[str] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            save(repo, "k/f.txt", f"v{i}".encode(), overwrite=False)
            results.append("ok")
        except AssetError:
            results.append("err")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1
    assert results.count("err") == 7


def test_saved_file_is_world_readable_per_umask(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    save(repo, "f.txt", b"x")
    mode = on_disk(tmp_path, "private", "f.txt").stat().st_mode & 0o777
    assert mode & 0o044  # nginx (other user) must be able to read it


def test_declared_length_and_checksum_are_verified(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    with pytest.raises(AssetError, match="content_length"):
        save(repo, "a.txt", b"abc", content_length=5)
    with pytest.raises(AssetError, match="Checksum"):
        save(repo, "a.txt", b"abc", checksum="md5:" + "0" * 32)
    assert not on_disk(tmp_path, "private", "a.txt").exists()
    save(
        repo,
        "a.txt",
        b"abc",
        content_length=3,
        checksum="md5:" + hashlib.md5(b"abc").hexdigest(),
    )


# ---- GA-008: limits ----------------------------------------------------------


def test_max_upload_bytes_enforced_while_streaming(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, max_upload_bytes=10)
    with pytest.raises(AssetError, match="maximum upload size"):
        save(repo, "big.txt", b"x" * 11)
    assert not on_disk(tmp_path, "private", "big.txt").exists()
    assert list((tmp_path / "assets" / "private").iterdir()) == []
    save(repo, "ok.txt", b"x" * 10)


def test_allowed_content_types(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        allowed_content_types=("image/*", "application/pdf"),
        upload_secret="u",
    )
    with pytest.raises(AssetError, match="not allowed"):
        save(repo, "a.txt", b"x")  # text/plain
    with pytest.raises(AssetError, match="not allowed"):
        repo.build_upload_url("a.html", "text/html")
    check_content_type("IMAGE/PNG; charset=x", ("image/*",))


def test_upload_token_v2_binds_type_and_visibility(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        upload_secret="u",
        upload_service_url="http://up",
        upload_token_format="v2",
    )
    res = repo.build_upload_url("k/a.png", "image/png", visibility=PUB)
    parsed = urlparse(res.url)
    _, _, expires, mac, b64 = parsed.path.split("/")
    assert base64.urlsafe_b64decode(b64 + "==").decode() == "k/a.png"
    q = parse_qs(parsed.query)
    assert q == {"visibility": ["public"], "content_type": ["image/png"]}
    payload = json.dumps(
        [int(expires), "k/a.png", "public", "image/png"], separators=(",", ":")
    )
    assert mac == hmac.new(b"u", payload.encode(), "sha256").hexdigest()
    forged = json.dumps(
        [int(expires), "k/a.png", "public", "image/svg+xml"], separators=(",", ":")
    )
    assert mac != hmac.new(b"u", forged.encode(), "sha256").hexdigest()


def test_upload_token_legacy_format_unchanged(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, upload_secret="u", upload_service_url="http://up")
    res = repo.build_upload_url("k/a.png", "image/png")
    _, _, expires, mac, _ = urlparse(res.url).path.split("/")
    assert mac == hmac.new(b"u", f"{expires}:k/a.png".encode(), "sha256").hexdigest()
    assert "?" not in res.url


def test_v2_tus_token_is_unambiguous(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path, upload_secret="u", tusd_url="http://t", upload_token_format="v2"
    )
    a = repo._build_upload_token("a:public", PRIV, "x", 1)
    b = repo._build_upload_token("a", PUB, "private:x", 1)
    assert a != b
    legacy = make_repo(tmp_path, upload_secret="u", tusd_url="http://t")
    # The legacy delimiter format cannot tell these two requests apart.
    assert legacy._build_upload_token(
        "a:public", PRIV, "x", 1
    ) == legacy._build_upload_token("a", PUB, "private:x", 1)


# ---- GA-006: URL encoding ----------------------------------------------------


def test_urls_encode_special_characters(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    key = "sp/a b?x=1#f%20.txt"
    save(repo, key, b"x", PRIV)
    url = repo.build_download_url(key).url
    parsed = urlparse(url)
    assert parsed.path == "/assets/private/sp/a%20b%3Fx%3D1%23f%2520.txt"
    assert set(parse_qs(parsed.query)) == {"md5", "expires"}  # no injected params
    # Token is computed over the decoded URI nginx sees in $uri.
    q = parse_qs(parsed.query)
    raw = f"{q['expires'][0]}{unquote(parsed.path)} s3cret".encode()
    digest = hashlib.md5(raw).digest()  # noqa: S324
    assert q["md5"][0] == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    save(repo, "p/a b.txt", b"x", PUB)
    assert (
        repo.build_public_url("p/a b.txt").url == "http://h/assets/public/p/a%20b.txt"
    )


# ---- GA-014 / GA-016 ----------------------------------------------------------


def test_resolve_key_edge_cases() -> None:
    assert resolve_asset_key("dir/", "f.txt") == "dir/dir.txt"
    assert resolve_asset_key("a", "f.t/x%") == "a/a"
    assert resolve_asset_key("a", "f.bad_ext") == "a/a"
    assert resolve_asset_key("informes/v1.2", None) == "informes/v1.2"


def test_validate_key_rejects_empty_segments_and_control_chars() -> None:
    for bad in ("a//b", "a/b/", "a\nb", "a/../b"):
        with pytest.raises(AssetError):
            validate_key(bad)


def test_exists_error_message_uses_resolved_key(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    save(repo, "dir", b"x", overwrite=True)  # resolved to dir/dir
    with pytest.raises(AssetError, match="dir/dir"):
        repo.save(
            AssetSaveRequest(
                source=b"y", content_type="text/plain", key="dir", overwrite=False
            )
        )
