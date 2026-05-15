Installation
============

Requirements
------------

* **Python**: 3.12 or higher
* **boto3**: 1.34 or higher (required for the S3 backend)

Package Installation
--------------------

**Using pip**:

.. code-block:: bash

   pip install granite-assets

**Using UV** (fastest):

.. code-block:: bash

   uv add granite-assets

**Using Poetry**:

.. code-block:: bash

   poetry add granite-assets

Development Installation
------------------------

.. code-block:: bash

   git clone https://github.com/your-org/granite-assets.git
   cd granite-assets
   uv sync

S3 Extra
--------

The S3 backend depends on ``boto3``, which is already declared as a hard
dependency.  No extra installation step is required.  If you are deploying in
a Lambda or container environment and want to trim the package size, you can
omit boto3 from your image and only import ``LocalNginxAssetRepository``.

.. code-block:: bash

   pip install granite-assets   # boto3 included

Database Migrations Extra
--------------------------

If you want to run granite-assets' built-in Alembic migrations from your host
application (for future asset metadata storage), install the ``db`` extra:

.. code-block:: bash

   pip install "granite-assets[db]"

   # or with uv:
   uv add "granite-assets[db]"

This pulls in ``sqlalchemy>=2.0`` and ``alembic>=1.13``.  See
:doc:`migrations` for usage details.

Type Checking
-------------

``granite-assets`` ships a ``py.typed`` marker, so mypy and pyright will pick
up the inline type annotations automatically without any additional
configuration.
