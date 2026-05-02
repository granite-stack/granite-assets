User Guide
==========

This guide covers the core concepts, the full API surface, and integration
patterns for both the local and S3 backends.

Architecture Overview
---------------------

Granite Assets has three layers:

``IAssetRepository`` (protocol)
    The structural interface your application depends on.  No inheritance
    required — any class implementing the methods qualifies at runtime
    (``@runtime_checkable`` Protocol).

Configuration dataclasses
    ``LocalNginxAssetRepositoryConfig`` and ``S3AssetRepositoryConfig`` are
    frozen-like dataclasses that hold all wiring details.  Pass one to
    ``build_asset_repository()`` and you get the right implementation back.

Concrete repositories
    ``LocalNginxAssetRepository`` — writes files to a directory tree served
    by any static HTTP server.
    ``S3AssetRepository`` — reads/writes from AWS S3 (or any S3-compatible store).

All methods are **synchronous**.  Use them inside a thread pool (e.g.
``asyncio.to_thread`` / ``run_in_executor``) when calling from an async context
such as FastAPI.

Key Concepts
------------

**Asset key**
    A forward-slash separated path that uniquely identifies an asset within the
    repository (e.g. ``"invoices/2024/inv-001.pdf"``).  Keys must **not** start
    with a leading slash.  The key is the stable identifier you store in your
    database — the physical location (filesystem path, S3 key with prefix) is
    an internal detail of the repository.

**AssetVisibility**
    ``PUBLIC`` — the asset is accessible via a stable, non-expiring URL.
    ``PRIVATE`` — the asset requires a time-limited signed URL.

    Visibility is set at write time in ``AssetSaveRequest`` and is reflected in
    ``AssetSaveResult`` and ``AssetDescriptor``.

**AssetSaveRequest**
    The input model for ``save()``.  The ``source`` field accepts either a
    ``BinaryIO`` stream or ``bytes``.  Call ``request.open_source()`` to always
    get a stream regardless of which was provided.

**AssetSaveResult**
    Returned by ``save()``.  Contains the ``key``, a backend-specific
    ``backend_ref`` (e.g. S3 ETag, absolute file path), ``content_length``, and
    ``checksum``.

**AssetDescriptor**
    Returned by ``get_descriptor()``.  Provides metadata without downloading
    the asset body — equivalent to an HTTP HEAD request.

**AssetAccessUrl**
    Returned by ``build_public_url()`` and ``build_download_url()``.  The
    ``url`` field is always populated.  ``expires_at`` is ``None`` for
    permanent public URLs.  Check ``url.is_permanent`` as a convenience.

**UploadUrlResult**
    Returned by ``build_upload_url()``.  Contains the presigned ``url``, the
    HTTP ``method`` (always ``"PUT"``), required ``headers``, ``expires_at``,
    and the ``key`` that will be created.

Saving Assets
-------------

.. code-block:: python

   import io
   from granite_assets import AssetSaveRequest, AssetVisibility

   # From an open file
   with open("report.pdf", "rb") as f:
       result = repo.save(AssetSaveRequest(
           key="reports/q1-2024.pdf",
           source=f,
           content_type="application/pdf",
           visibility=AssetVisibility.PRIVATE,
           filename="q1-2024.pdf",
           metadata={"uploader": "user-123"},
       ))

   # From bytes
   result = repo.save(AssetSaveRequest(
       key="thumbnails/user-42.jpg",
       source=thumbnail_bytes,
       content_type="image/jpeg",
       visibility=AssetVisibility.PUBLIC,
   ))

   # Prevent overwriting an existing asset
   result = repo.save(AssetSaveRequest(
       key="config/settings.json",
       source=json_bytes,
       content_type="application/json",
       overwrite=False,   # raises AssetError if the key already exists
   ))

Reading Asset Metadata
----------------------

.. code-block:: python

   # Check existence cheaply
   if repo.exists("reports/q1-2024.pdf"):
       desc = repo.get_descriptor("reports/q1-2024.pdf")
       print(desc.content_type)
       print(desc.content_length)
       print(desc.last_modified)
       print(desc.visibility)

Copy and Move
-------------

Copy and move are cheap server-side operations — the library never downloads
the asset body just to re-upload it.

.. code-block:: python

   # Copy to another key
   repo.copy("reports/q1-2024.pdf", "archive/2024/q1.pdf")

   # Move (rename)
   repo.move("reports/draft.pdf", "reports/final.pdf")

   # Both accept overwrite control
   repo.copy("src.jpg", "dst.jpg", overwrite=False)

Deleting Assets
---------------

.. code-block:: python

   repo.delete("thumbnails/user-42.jpg")  # raises AssetNotFoundError if missing

FastAPI Integration
-------------------

Since all repository methods are synchronous, wrap them in
``asyncio.to_thread`` inside async endpoints:

.. code-block:: python

   import asyncio
   from fastapi import FastAPI, UploadFile
   from granite_assets import AssetSaveRequest, AssetVisibility

   app = FastAPI()

   @app.post("/upload")
   async def upload_file(file: UploadFile) -> dict:
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
       dl_url = await asyncio.to_thread(
           repo.build_download_url, result.key, 600
       )
       return {"url": dl_url.url, "expires_at": dl_url.expires_at}

Local Nginx Backend Details
-----------------------------

Files are organised under two sub-directories inside ``storage_path``:

* ``<storage_path>/<public_prefix>/<key>`` — publicly served.
* ``<storage_path>/<private_prefix>/<key>`` — should be protected by Nginx
  ``auth_request`` or similar.

Presigned URLs are **not supported** by this backend.  Calling
``build_download_url()`` on a public asset returns a permanent URL (same as
``build_public_url()``).  Calling ``build_upload_url()`` raises
``AssetAccessNotSupportedError``.

Example Nginx configuration:

.. code-block:: nginx

   location /assets/public/ {
       alias /var/www/assets/public/;
   }

   location /assets/private/ {
       internal;
       alias /var/www/assets/private/;
   }

S3 Backend Details
------------------

* **ACLs** — the library sets ``ACL='public-read'`` for public objects when
  saving.  Ensure your bucket allows ACLs or manage access via bucket policy
  instead and set ``public_base_url`` to your CDN domain.
* **Custom endpoint** — set ``endpoint_url`` for MinIO, LocalStack, or any
  S3-compatible store.
* **Credentials** — pass ``access_key_id`` / ``secret_access_key`` directly or
  leave them as ``None`` to use the standard boto3 credential chain (environment
  variables, instance roles, ``~/.aws/credentials``).
* **Key prefix** — ``key_prefix`` is prepended to the logical key before writing
  to S3 and stripped when reading back.  Application code always works with the
  unprefixed logical key.
