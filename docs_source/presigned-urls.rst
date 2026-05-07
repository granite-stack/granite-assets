Upload and Download URLs
========================

Both backends support generating time-limited URLs that let a client upload or
download assets **without routing binary data through your application server**.
The upload protocol differs between backends:

+-------------------+------------------------+--------------------------------------+
| Backend           | Upload protocol        | Download (private)                   |
+===================+========================+======================================+
| ``S3``            | Presigned ``PUT``      | Presigned ``GET`` (S3 or CloudFront) |
+-------------------+------------------------+--------------------------------------+
| ``LocalNginx``    | tus ``POST + PATCH``   | Nginx ``secure_link`` signed URL     |
+-------------------+------------------------+--------------------------------------+

Both backends return the same ``UploadUrlResult`` model.  Your application code
and client-side code can be written against the common interface and swapped at
configuration time.

.. contents:: On this page
   :local:
   :depth: 2

----

Upload URLs
-----------

S3 — Presigned PUT
~~~~~~~~~~~~~~~~~~

The classic S3 presigned upload flow has three steps:

1. **Request a presigned URL** from your backend API.
2. **PUT the file directly to S3** using the returned URL and headers.
3. **Confirm the upload** — your backend polls ``exists()`` or receives an S3
   event notification.

.. code-block:: python

   from granite_assets import S3AssetRepositoryConfig, build_asset_repository

   config = S3AssetRepositoryConfig(
       bucket="my-bucket",
       region="eu-west-1",
       presign_ttl_seconds=900,   # 15 minutes
   )
   repo = build_asset_repository(config)

   upload = repo.build_upload_url(
       key="uploads/user-42/avatar.jpg",
       content_type="image/jpeg",
       ttl_seconds=600,   # override config default
   )

   print(upload.url)        # https://my-bucket.s3.eu-west-1.amazonaws.com/...
   print(upload.method)     # "PUT"
   print(upload.headers)    # {"Content-Type": "image/jpeg"}
   print(upload.expires_at) # datetime (UTC)
   print(upload.key)        # "uploads/user-42/avatar.jpg"

Local Nginx — tus (via tusd)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The local backend uses the `tus resumable upload protocol <https://tus.io>`_
with a `tusd <https://github.com/tus/tusd>`_ server.  This supports
arbitrarily large files and automatic resume on connection loss.

The upload flow has three steps:

1. **Request a tus creation URL** from your backend API.
2. **POST** to create the upload resource; receive a ``Location`` header.
3. **PATCH** the file data in one or more chunks to the ``Location`` URL.

Tusd must be configured and running.  Set ``tusd_url`` and ``upload_secret``
on the repository config:

.. code-block:: python

   import os
   from granite_assets import LocalNginxAssetRepositoryConfig, build_asset_repository

   config = LocalNginxAssetRepositoryConfig(
       storage_path="/srv/assets",
       base_url="https://media.example.com/assets",
       tusd_url="http://localhost:1080",           # or internal service name
       upload_secret=os.environ["UPLOAD_SECRET"],
       upload_ttl_seconds=3600,
   )
   repo = build_asset_repository(config)

   upload = repo.build_upload_url(
       key="invoices/inv-001.pdf",
       content_type="application/pdf",
       visibility=AssetVisibility.PRIVATE,
   )

   print(upload.url)     # http://localhost:1080/files/
   print(upload.method)  # "POST"
   print(upload.headers)
   # {
   #   "Tus-Resumable": "1.0.0",
   #   "Upload-Metadata": "asset-key aW52…, content-type YXBw…, …",
   #   "Content-Length": "0",
   # }

.. note::

   ``LocalNginxAssetRepositoryConfig`` requires **both** ``tusd_url`` and
   ``upload_secret`` to be set.  Omitting either raises
   ``AssetAccessNotSupportedError``.  If you do not need upload URL support,
   simply leave these fields unset.

The ``Upload-Metadata`` header embeds five fields (base64-encoded values):

+--------------------+------------------------------------------+
| Field              | Content                                  |
+====================+==========================================+
| ``asset-key``      | Logical key for the asset                |
+--------------------+------------------------------------------+
| ``content-type``   | MIME type                                |
+--------------------+------------------------------------------+
| ``visibility``     | ``"public"`` or ``"private"``            |
+--------------------+------------------------------------------+
| ``upload-expires`` | Unix timestamp when the token expires    |
+--------------------+------------------------------------------+
| ``upload-token``   | HMAC-SHA256 hex digest (see below)       |
+--------------------+------------------------------------------+

The upload token is computed as::

   payload  = f"{expires}:{key}:{visibility}:{content_type}"
   token    = hmac.new(upload_secret, payload, "sha256").hexdigest()

Your tusd ``pre-create`` hook must replicate this computation to validate
incoming uploads.  See :doc:`infrastructure` for a complete hook example.

Common FastAPI Endpoint (works with both backends)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``UploadUrlResult`` model is identical for both backends.  You can write
a single endpoint that serves both:

.. code-block:: python

   import asyncio
   from fastapi import FastAPI
   from pydantic import BaseModel
   from datetime import datetime

   app = FastAPI()

   class UploadUrlResponse(BaseModel):
       url: str
       method: str
       headers: dict[str, str]
       expires_at: datetime
       key: str

   @app.get("/api/upload-url", response_model=UploadUrlResponse)
   async def get_upload_url(key: str, content_type: str) -> UploadUrlResponse:
       # repo can be LocalNginxAssetRepository or S3AssetRepository —
       # the calling code is identical either way.
       result = await asyncio.to_thread(repo.build_upload_url, key, content_type)
       return UploadUrlResponse(
           url=result.url,
           method=result.method,
           headers=result.headers,
           expires_at=result.expires_at,
           key=result.key,
       )

Client-Side Upload (JavaScript)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For S3 (``method = "PUT"``):

.. code-block:: javascript

   const { url, method, headers, key } = await fetch(
       '/api/upload-url?' + new URLSearchParams({ key, content_type })
   ).then(r => r.json());

   // Simple PUT (S3 presigned)
   await fetch(url, { method, headers, body: fileBlob });

   await fetch('/api/confirm', { method: 'POST',
       body: JSON.stringify({ key }) });

For tusd (``method = "POST"``), use the `tus-js-client <https://github.com/tus/tus-js-client>`_
library which handles creation + chunked PATCH automatically:

.. code-block:: javascript

   import { Upload } from 'tus-js-client';

   const { url, headers, key } = await fetch(
       '/api/upload-url?' + new URLSearchParams({ key, content_type })
   ).then(r => r.json());

   const upload = new Upload(file, {
       endpoint: url,
       headers: headers,         // includes Tus-Resumable + Upload-Metadata
       removeFingerprintOnSuccess: true,
       onSuccess: () => fetch('/api/confirm', { method: 'POST',
           body: JSON.stringify({ key }) }),
       onError: (err) => console.error('Upload failed:', err),
   });
   upload.start();

----

Download URLs
-------------

S3 — Three URL modes for private assets
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The S3 backend supports three URL modes for ``build_download_url``, selected
by the configuration fields you set.  See :ref:`download_url_modes` in the
infrastructure guide for a full comparison.

**Mode 1 — CloudFront signed URL** (recommended for production)

Requires a CloudFront key pair and ``trusted_key_groups`` on the cache
behavior.  The URL is time-limited and never exposes the S3 domain.

.. code-block:: python

   import os
   from granite_assets import S3AssetRepositoryConfig, build_asset_repository

   config = S3AssetRepositoryConfig(
       bucket="my-bucket",
       region="eu-west-1",
       public_base_url="https://d111….cloudfront.net",
       cf_key_id=os.environ["CF_KEY_ID"],
       cf_private_key=open("/path/to/private_key.pem").read(),
       presign_ttl_seconds=3600,
   )
   repo = build_asset_repository(config)

   dl = repo.build_download_url("invoices/inv-001.pdf", ttl_seconds=300)
   print(dl.url)          # https://d111….cloudfront.net/private/…?Expires=…&Signature=…
   print(dl.expires_at)   # datetime (UTC)
   print(dl.is_permanent) # False

**Mode 2 — Plain CloudFront URL** (permanent, no signing)

Set ``cf_unsigned_urls=True`` when no viewer-access restriction is required.
The URL is permanent; protect access at the application layer.

.. code-block:: python

   config = S3AssetRepositoryConfig(
       bucket="my-bucket",
       region="eu-west-1",
       public_base_url="https://d111….cloudfront.net",
       cf_unsigned_urls=True,
   )
   repo = build_asset_repository(config)

   dl = repo.build_download_url("invoices/inv-001.pdf")
   print(dl.url)          # https://d111….cloudfront.net/private/invoices/inv-001.pdf
   print(dl.is_permanent) # True

**Mode 3 — S3 presigned URL** (fallback, no CloudFront fields set)

.. code-block:: python

   config = S3AssetRepositoryConfig(
       bucket="my-bucket",
       region="eu-west-1",
       presign_ttl_seconds=300,
   )
   repo = build_asset_repository(config)

   dl = repo.build_download_url("invoices/inv-001.pdf", ttl_seconds=300)
   print(dl.url)          # https://my-bucket.s3.eu-west-1.amazonaws.com/...
   print(dl.expires_at)   # datetime (UTC)
   print(dl.is_permanent) # False

Local Nginx — Secure Link
~~~~~~~~~~~~~~~~~~~~~~~~~

Private assets served by Nginx are protected with
``ngx_http_secure_link_module``.  The token is an MD5 digest of
``"{expires}{uri} {secret}"``, URL-safe base64-encoded without padding —
exactly what Nginx verifies server-side.

Activate secure link support by setting ``secure_link_secret``:

.. code-block:: python

   import os
   from granite_assets import LocalNginxAssetRepositoryConfig, build_asset_repository

   config = LocalNginxAssetRepositoryConfig(
       storage_path="/srv/assets",
       base_url="https://media.example.com/assets",
       secure_link_secret=os.environ["SECURE_LINK_SECRET"],
       secure_link_ttl_seconds=3600,
   )
   repo = build_asset_repository(config)

   # Public asset — permanent URL, no token
   pub = repo.build_public_url("avatars/user-42.jpg")
   print(pub.url)          # https://media.example.com/assets/public/avatars/...
   print(pub.is_permanent) # True

   # Private asset — Nginx secure_link URL
   dl = repo.build_download_url("invoices/inv-001.pdf", ttl_seconds=300)
   print(dl.url)
   # https://media.example.com/assets/private/invoices/inv-001.pdf
   #         ?md5=TOKEN&expires=1747000000
   print(dl.is_permanent) # False

.. note::

   If ``secure_link_secret`` is not configured, calling
   ``build_download_url()`` on a private asset raises
   ``AssetAccessNotSupportedError``.  You must then proxy downloads through
   your application layer and stream the file body manually.

----

Security Considerations
-----------------------

* **Validate the asset key** before generating any URL.  Never pass
  user-supplied strings directly as the key — strip path traversal sequences
  (``..``, leading slashes) and enforce an allowlist of characters.
* **Use the shortest practical TTL.**  The default of 3600 s is generous;
  for one-time download links, 60–300 s is often sufficient.
* **S3 Content-Type enforcement** — the ``Content-Type`` header is part of
  the presigned request signature.  A client that sends a different
  content-type will receive ``403 Forbidden`` from S3.
* **S3 IP restriction** — optionally restrict presigned URLs to the
  expected client IP via an S3 bucket policy condition (``aws:SourceIp``).
* **tusd upload-token validation** — always implement the ``pre-create``
  hook to verify the HMAC-SHA256 ``upload-token`` in ``Upload-Metadata``.
  Without this, any client that can reach the tusd port could upload
  arbitrary files.  Use ``hmac.compare_digest`` (constant-time) for the
  comparison.
* **upload_secret rotation** — rotate ``upload_secret`` independently of
  ``secure_link_secret``; they serve different trust boundaries (write vs read).
