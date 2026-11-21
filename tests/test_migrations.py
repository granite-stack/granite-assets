"""Tests for granite_assets.migrations.upgrade_to_head (GA-020)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("alembic")
pytest.importorskip("sqlalchemy")

from granite_assets.migrations import _redact_url, upgrade_to_head  # noqa: E402


def _tables(db: Path) -> set[str]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute("select name from sqlite_master").fetchall()
    return {r[0] for r in rows}


def test_upgrade_creates_dedicated_version_table(tmp_path: Path) -> None:
    db = tmp_path / "plain.db"
    upgrade_to_head(f"sqlite:///{db}")
    assert "granite_assets_alembic_version" in _tables(db)
    assert "alembic_version" not in _tables(db)
    upgrade_to_head(f"sqlite:///{db}")  # idempotent


def test_upgrade_accepts_percent_in_url(tmp_path: Path) -> None:
    """URL-encoded characters ('%') used to raise ValueError in configparser."""
    db = tmp_path / "a b%.db"
    upgrade_to_head(f"sqlite:///{tmp_path}/a%20b%25.db")
    assert "granite_assets_alembic_version" in _tables(db)


def test_redact_url_hides_password() -> None:
    assert "secret" not in _redact_url("postgresql://u:secret@h/db")
