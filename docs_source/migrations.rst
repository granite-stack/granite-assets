Database Migrations
===================

granite-assets ships with a built-in `Alembic <https://alembic.sqlalchemy.org>`_
configuration so that its own schema migrations — added as it gains database
features such as asset metadata storage — can be applied directly from host
applications without any extra configuration.

.. contents:: On this page
   :local:
   :depth: 2


How it works
------------

When you install granite-assets and call ``upgrade_to_head()``, Alembic runs the
revision files bundled **inside** the wheel (``granite_assets/alembic/versions/``)
against your database.  A dedicated version table called
``granite_assets_alembic_version`` is used, completely separate from the
``alembic_version`` table that your own project manages.  The two revision
histories never interfere with each other.

.. note::

   Until granite-assets gains SQLAlchemy models, the function is a
   **no-op** (or near-no-op): it simply stamps the version table if it does
   not exist yet, then returns immediately.  It is safe to call on every
   deployment.


Installation
------------

The migration helpers require ``alembic`` and ``sqlalchemy``, which are
**not** included in the default install to keep the footprint small.
Install the ``db`` optional extra::

   pip install "granite-assets[db]"

   # or with uv:
   uv add "granite-assets[db]"


Calling ``upgrade_to_head()``
------------------------------

.. code-block:: python

   from granite_assets.migrations import upgrade_to_head

   # Pass a *synchronous* SQLAlchemy URL.
   # Async drivers (asyncpg, aiosqlite) must be converted first — see below.
   upgrade_to_head("postgresql+psycopg2://user:pass@localhost/mydb")

If your application uses an async driver (e.g. ``asyncpg``), replace the
scheme before passing it to ``upgrade_to_head()``:

.. code-block:: python

   db_url = settings.db_url  # "postgresql+asyncpg://..."
   sync_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
   upgrade_to_head(sync_url)


Integration with host application migrations
---------------------------------------------

The recommended pattern is to run granite-assets migrations **before** the
host application's own Alembic upgrade so that any foreign-key references
to granite-assets tables are satisfied when the host schema is applied.

**scripts/migrate.py** (create one in your project):

.. code-block:: python

   from granite_assets.migrations import upgrade_to_head
   from alembic.config import Config
   from alembic import command

   def main(sync_url: str) -> None:
       # 1. granite-assets schema first
       upgrade_to_head(sync_url)

       # 2. host application schema second
       cfg = Config("alembic.ini")
       command.upgrade(cfg, "head")

Then wire it in your ``Makefile``::

   migrate:
       uv run python scripts/migrate.py


Adding new revisions (for granite-assets maintainers)
-------------------------------------------------------

When a new SQLAlchemy model is added to granite-assets, create a revision
file inside the bundled alembic directory:

.. code-block:: bash

   cd /path/to/granite-assets
   uv run alembic --config src/granite_assets/alembic/alembic.ini \
       revision --autogenerate -m "add asset metadata table"

Or, if you have a convenience wrapper in ``makefile``::

   make migrate-create MSG="add asset metadata table"

The generated file lands in
``src/granite_assets/alembic/versions/`` and is included in the next
wheel build automatically.


.. tip::

   The ``version_table = "granite_assets_alembic_version"`` setting in
   ``env.py`` ensures that running the above command against a database
   that already contains a host application's ``alembic_version`` table
   does not overwrite or corrupt it.
