"""Alembic environment for granite-assets internal migrations.

This file is bundled inside the granite-assets package and is invoked by
``granite_assets.migrations.upgrade_to_head()``.  It does NOT need to be
configured by the calling project.

When granite-assets adds SQLAlchemy models (e.g., for asset metadata storage),
their ``Base.metadata`` will be imported here.  Until then the target is an
empty ``MetaData`` object, so running this against any database is a no-op that
only stamps the alembic_version table.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import MetaData, engine_from_config, pool

# ---------------------------------------------------------------------------
# Target metadata
# ---------------------------------------------------------------------------
# Replace this with the actual SQLAlchemy metadata when DB models are added:
#
#   from granite_assets.db import Base
#   target_metadata = Base.metadata
#
target_metadata = MetaData()

# ---------------------------------------------------------------------------
# Alembic config — set by upgrade_to_head() before env.py is executed
# ---------------------------------------------------------------------------
config = context.config

if config.config_file_name is not None:  # pragma: no cover
    fileConfig(config.config_file_name)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (no live DB connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        version_table="granite_assets_alembic_version",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (live DB connection)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # Use a separate version table to avoid colliding with the
            # calling project's own alembic_version table.
            version_table="granite_assets_alembic_version",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
