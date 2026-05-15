"""granite-assets database migration helpers.

This module exposes ``upgrade_to_head()`` for use by host applications
(lms-backend, granite-backend, etc.) that want to apply granite-assets'
own schema migrations alongside their own Alembic workflow.

Usage
-----
Call ``upgrade_to_head()`` from your project's migration script **before**
running your own Alembic upgrade::

    from granite_assets.migrations import upgrade_to_head

    upgrade_to_head("postgresql+psycopg2://user:pass@localhost/mydb")

The function is intentionally synchronous so it works in both sync and
async application contexts without requiring event-loop management.

Version table
-------------
granite-assets uses a dedicated ``granite_assets_alembic_version`` table,
completely separate from the ``alembic_version`` table used by the host
application.  There is no conflict or interference between the two.

No-op behaviour
---------------
When no revision files are present (granite-assets has no DB models yet),
this function stamps the version table and returns immediately.  It is safe
to call on every deployment.

Requirements
------------
This module requires the ``alembic`` and ``sqlalchemy`` packages.  Install
them via the ``db`` optional extra::

    pip install "granite-assets[db]"
"""

from __future__ import annotations

import importlib.resources
import logging

logger = logging.getLogger(__name__)


def upgrade_to_head(database_url: str) -> None:
    """Apply all pending granite-assets migrations to *database_url*.

    Args:
        database_url: A synchronous SQLAlchemy database URL, e.g.
            ``"postgresql+psycopg2://user:pass@localhost/mydb"``.
            Async drivers (``asyncpg``) are **not** supported here —
            Alembic's synchronous engine is used internally regardless
            of what the host application uses.

    Raises:
        ImportError: If ``alembic`` or ``sqlalchemy`` are not installed.
        sqlalchemy.exc.SQLAlchemyError: On any database error.
    """
    try:
        from alembic import command
        from alembic.config import Config
    except ImportError as exc:
        raise ImportError(
            "alembic is required to run granite-assets migrations. "
            "Install it with: pip install 'granite-assets[db]'"
        ) from exc

    # Resolve the alembic directory bundled inside this package.
    # Works both from source and from an installed wheel.
    pkg_ref = importlib.resources.files("granite_assets")
    script_location = str(pkg_ref / "alembic")

    cfg = Config()
    cfg.set_main_option("script_location", script_location)
    cfg.set_main_option("sqlalchemy.url", database_url)

    logger.debug(
        "granite-assets: running alembic upgrade head on %s",
        _redact_url(database_url),
    )
    command.upgrade(cfg, "head")
    logger.debug("granite-assets: migrations complete")


def _redact_url(url: str) -> str:
    """Return the URL with the password replaced by ***."""
    try:
        from sqlalchemy.engine import make_url

        u = make_url(url)
        return str(u.render_as_string(hide_password=True))
    except Exception:
        return "<url>"
