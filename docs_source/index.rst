Granite Assets Documentation
=============================

**Granite Assets** is a portable, backend-agnostic asset repository abstraction
for Python applications.  It provides a single, typed ``IAssetRepository`` interface
with two ready-made implementations — local filesystem (Nginx-served) and AWS S3 —
so you can swap storage backends without touching application code.

.. image:: https://img.shields.io/pypi/v/granite-assets.svg
   :target: https://pypi.org/project/granite-assets/
   :alt: PyPI Version

.. image:: https://img.shields.io/pypi/pyversions/granite-assets.svg
   :target: https://pypi.org/project/granite-assets/
   :alt: Python Version

.. image:: https://img.shields.io/badge/license-MIT-blue.svg
   :target: https://github.com/your-org/granite-assets/blob/main/LICENSE
   :alt: License

Key Features
============

* **Protocol-based contract** — implement ``IAssetRepository`` without inheriting from any base class.
* **Local + S3 backends** — production-ready implementations included.
* **Resumable uploads (tus)** — ``LocalNginxAssetRepository`` generates tus creation URLs for a `tusd <https://github.com/tus/tusd>`_ server, supporting arbitrarily large files and resume on failure.
* **Presigned S3 upload URLs** — ``S3AssetRepository`` generates presigned ``PUT`` URLs for direct browser-to-S3 uploads.
* **Signed download URLs** — ``LocalNginxAssetRepository`` supports Nginx ``secure_link`` tokens; ``S3AssetRepository`` generates presigned ``GET`` URLs.
* **Public URL support** — stable, permanent URLs for public assets via CDN or Nginx.
* **Asset descriptors** — lightweight metadata queries (``HEAD``-like) without downloading content.
* **Copy / move** — cheap server-side operations that avoid unnecessary data transfer.
* **Typed** — full type hints; ``py.typed`` marker included.
* **Extensible** — add any new backend by implementing the ``IAssetRepository`` protocol.

Quick Start
===========

Installation::

   pip install granite-assets

Local filesystem example:

.. code-block:: python

   from granite_assets import (
       LocalNginxAssetRepositoryConfig,
       LocalNginxAssetRepository,
       AssetSaveRequest,
       AssetVisibility,
   )

   config = LocalNginxAssetRepositoryConfig(
       storage_path="/var/www/assets",
       base_url="https://cdn.example.com/assets",
   )
   repo = LocalNginxAssetRepository(config)

   request = AssetSaveRequest(
       key="avatars/user-42.jpg",
       source=open("photo.jpg", "rb"),
       content_type="image/jpeg",
       visibility=AssetVisibility.PUBLIC,
   )
   result = repo.save(request)
   url = repo.build_public_url(result.key)
   print(url.url)  # https://cdn.example.com/assets/public/avatars/user-42.jpg

Contents
========

.. toctree::
   :maxdepth: 2
   :caption: Getting Started

   installation
   quickstart

.. toctree::
   :maxdepth: 2
   :caption: User Guide

   user-guide
   presigned-urls
   infrastructure

.. toctree::
   :maxdepth: 2
   :caption: Developer Guide

   implementing-repository
   migrations

.. toctree::
   :maxdepth: 2
   :caption: API Reference

   api

Indices and Tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
