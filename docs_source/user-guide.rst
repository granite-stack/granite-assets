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
    repository.  Keys must **not** start with a leading slash.  The key is the
    stable identifier you store in your database — the physical location
    (filesystem path, S3 key with prefix) is an internal detail of the
    repository.

    ``key`` is **optional** in ``AssetSaveRequest``.  When omitted, the
    repository auto-generates a collision-free key using a UUID folder
    structure (see :ref:`auto-key-layout` below).

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
    Returned by ``build_upload_url()``.  Contains the ``url``, the HTTP
    ``method`` (``"PUT"`` for S3, ``"POST"`` for tus), required ``headers``,
    ``expires_at``, and the ``key`` that will be created after a successful
    upload.  See :doc:`presigned-urls` for the full upload flow.

Saving Assets
-------------

.. code-block:: python

   import io
   from granite_assets import AssetSaveRequest, AssetVisibility

   # From an open file — explicit key
   with open("report.pdf", "rb") as f:
       result = repo.save(AssetSaveRequest(
           key="reports/q1-2024.pdf",
           source=f,
           content_type="application/pdf",
           visibility=AssetVisibility.PRIVATE,
           filename="q1-2024.pdf",
           metadata={"uploader": "user-123"},
       ))

   # Auto-generated key — omit ``key`` and supply ``filename``
   result = repo.save(AssetSaveRequest(
       source=thumbnail_bytes,
       content_type="image/jpeg",
       filename="photo.jpg",
       visibility=AssetVisibility.PUBLIC,
   ))
   print(result.key)
   # e.g. "3b105bc5-6056-4a52-b03b-7d953644c826/3b105bc5-6056-4a52-b03b-7d953644c826.jpg"

   # Prefix-only key — pass a path without extension to place the file inside
   # a named folder; granite-assets appends the UUID subfolder automatically
   result = repo.save(AssetSaveRequest(
       key="private/3b105bc5-6056-4a52-b03b-7d953644c826",
       source=data,
       content_type="image/png",
       filename="photo.png",
   ))
   print(result.key)
   # "private/3b105bc5-.../3b105bc5-....png"

   # Prevent overwriting an existing asset
   result = repo.save(AssetSaveRequest(
       key="config/settings.json",
       source=json_bytes,
       content_type="application/json",
       overwrite=False,   # raises AssetError if the key already exists
   ))

.. _auto-key-layout:

Auto-generated Key Layout
--------------------------

When ``key`` is omitted from ``AssetSaveRequest``, both backends enforce a
collision-free, privacy-safe storage layout::

   {uuid}/{uuid}.{ext}

The *same* UUID is used for the folder and the filename.  This means:

* The folder name is the stable identifier for the asset — you can sign a
  CloudFront wildcard resource for ``{uuid}/*`` to grant access to all
  representations of that asset (thumbnail, original, HLS segments …)
  with a single policy.
* Physical paths are never guessable or enumerable.
* No collision is possible regardless of original filename.

Three key resolution rules are applied by ``save()``:

1. **``key`` is ``None``** → generate ``{uuid}/{uuid}.ext`` using a new UUID.
2. **``key`` has no file extension** → treat it as a folder prefix and
   append ``/{last_segment}.ext``.  Useful when the caller pre-allocates a
   UUID and wants to delegate the path construction to the repository.
3. **``key`` has a file extension** → use it unchanged (backward-compatible).

The extension is derived from ``filename``; when ``filename`` is also absent,
no extension is appended (``{uuid}/{uuid}``).

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
* ``<storage_path>/<private_prefix>/<key>`` — private assets (Nginx-protected).

Public URLs are constructed by joining ``base_url``, the relevant prefix, and
the logical key.

**Signed download URLs (secure_link)**

Set ``secure_link_secret`` to enable time-limited download URLs for private
assets.  The token algorithm matches ``ngx_http_secure_link_module``:

.. code-block:: python

   import os
   config = LocalNginxAssetRepositoryConfig(
       storage_path="/srv/assets",
       base_url="https://media.example.com/assets",
       secure_link_secret=os.environ["SECURE_LINK_SECRET"],
       secure_link_ttl_seconds=3600,   # default TTL; override per-call
   )

Without ``secure_link_secret``, calling ``build_download_url()`` on a private
asset raises ``AssetAccessNotSupportedError`` — you must proxy downloads
through your application layer.

**Resumable upload URLs (tus / tusd)**

Set ``tusd_url`` and ``upload_secret`` to enable ``build_upload_url()``.  The
method returns a tus *creation* URL (``method="POST"``) with signed
``Upload-Metadata``.  The tusd ``pre-create`` hook must verify the
``upload-token`` field using the same ``upload_secret``:

.. code-block:: python

   config = LocalNginxAssetRepositoryConfig(
       storage_path="/srv/assets",
       base_url="https://media.example.com/assets",
       secure_link_secret=os.environ["SECURE_LINK_SECRET"],
       tusd_url="http://localhost:1080",
       upload_secret=os.environ["UPLOAD_SECRET"],
       upload_ttl_seconds=3600,
   )

See :doc:`presigned-urls` for the full upload flow and hook implementation,
and :doc:`infrastructure` for docker-compose and Nginx configuration.

Example Nginx configuration:

.. code-block:: nginx

   location /assets/public/ {
       alias /var/www/assets/public/;
   }

   location /assets/private/ {
       # Validate secure_link token
       secure_link $arg_md5,$arg_expires;
       secure_link_md5 "$secure_link_expires$uri YOUR_SECRET_HERE";
       if ($secure_link = "")  { return 403; }
       if ($secure_link = "0") { return 410; }  # expired
       alias /var/www/assets/private/;
   }

S3 Backend Details
------------------

* **ACLs** — by default the library sets ``ACL='public-read'`` for public
  objects when saving.  If your bucket has *Object Ownership* set to
  ``BucketOwnerEnforced`` (ACLs disabled), set ``use_object_acl=False`` on
  ``S3AssetRepositoryConfig``.  Visibility is then controlled entirely via
  bucket policy or CloudFront OAC.

  .. code-block:: python

     config = S3AssetRepositoryConfig(
         bucket="my-bucket",
         region="eu-west-1",
         use_object_acl=False,   # required when ACLs are disabled on the bucket
     )

* **Custom endpoint** — set ``endpoint_url`` for MinIO, LocalStack, or any
  S3-compatible store.
* **Credentials** — pass ``access_key_id`` / ``secret_access_key`` directly or
  leave them as ``None`` to use the standard boto3 credential chain (environment
  variables, instance roles, ``~/.aws/credentials``).
* **Key prefix** — ``key_prefix`` is prepended to the logical key before writing
  to S3 and stripped when reading back.  Application code always works with the
  unprefixed logical key.

CloudFront Signed URLs for Streaming
-------------------------------------

For HLS/DASH streaming or any multi-file asset (e.g. thumbnails alongside an
original), use ``build_folder_signed_url()`` to issue a single custom-policy
signed URL that grants access to every file under the asset's UUID folder:

.. code-block:: python

   # The asset was saved at: private/3b105bc5-.../3b105bc5-....mp4
   # HLS segments live alongside it:  3b105bc5-....m3u8, seg-0.ts, seg-1.ts …

   stream_url = repo.build_folder_signed_url(
       key="private/3b105bc5-6056-4a52-b03b-7d953644c826/3b105bc5-....mp4",
       entry_filename="master.m3u8",
       ttl_seconds=3600,
   )
   # stream_url.url points to:  https://cdn.example.com/private/3b105bc5-.../master.m3u8
   # The CloudFront policy resource covers:  https://cdn.example.com/private/3b105bc5-.../*
   # so all segment files are accessible with the same signed credentials.

This requires ``cf_key_id`` and ``cf_private_key`` to be set in
``S3AssetRepositoryConfig``.
