Presigned URLs
==============

Presigned URLs let a client upload or download assets directly from S3 without
routing the binary data through your application server.  This reduces bandwidth
costs and latency for large files.

.. note::

   Presigned upload URLs (``build_upload_url``) are **only supported by the S3
   backend**.  ``LocalNginxAssetRepository`` raises
   ``AssetAccessNotSupportedError`` if you call this method.

   Presigned download URLs (``build_download_url``) on a local-nginx public
   asset return a permanent URL rather than an error.

Upload Flow (S3)
----------------

The client-side upload flow has three steps:

1. **Request a presigned URL** from your backend API.
2. **PUT the file directly to S3** using the returned URL and headers.
3. **Confirm the upload** — your backend can poll ``exists()`` or receive an S3
   event notification.

Generating a Presigned Upload URL
----------------------------------

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
   print(upload.headers)    # {"Content-Type": "image/jpeg", ...}
   print(upload.expires_at) # datetime (UTC)
   print(upload.key)        # "uploads/user-42/avatar.jpg"

The JSON you would return from a FastAPI endpoint:

.. code-block:: python

   from fastapi import FastAPI
   from pydantic import BaseModel
   from datetime import datetime

   class PresignedUploadResponse(BaseModel):
       url: str
       method: str
       headers: dict[str, str]
       expires_at: datetime
       key: str

   @app.get("/presign/upload")
   async def presign_upload(key: str, content_type: str) -> PresignedUploadResponse:
       result = await asyncio.to_thread(
           repo.build_upload_url, key, content_type
       )
       return PresignedUploadResponse(
           url=result.url,
           method=result.method,
           headers=result.headers,
           expires_at=result.expires_at,
           key=result.key,
       )

Client-side Upload (JavaScript)
--------------------------------

.. code-block:: javascript

   const { url, method, headers, key } = await fetch('/presign/upload?' + new URLSearchParams({
       key: 'uploads/user-42/avatar.jpg',
       content_type: 'image/jpeg',
   })).then(r => r.json());

   await fetch(url, {
       method: method,
       headers: headers,
       body: fileBlob,
   });

   // Notify your backend that the upload is complete
   await fetch('/media/confirm', {
       method: 'POST',
       body: JSON.stringify({ key }),
   });

Generating a Presigned Download URL
-------------------------------------

For private assets you need a time-limited URL to allow a specific user to
download the file:

.. code-block:: python

   dl = repo.build_download_url(
       key="invoices/inv-001.pdf",
       ttl_seconds=300,   # 5 minutes
   )
   print(dl.url)        # https://... (valid for 5 minutes)
   print(dl.expires_at) # datetime (UTC)
   print(dl.is_permanent) # False

Security Considerations
-----------------------

* **Validate key input** on your backend before issuing a presigned URL.
  Never pass user-controlled strings directly as the S3 key without sanitising
  path separators and disallowed characters.
* **Set the shortest TTL** that is practical for your use case.
* **Content-Type enforcement** — the ``Content-Type`` header is part of the
  signed request.  A client that sends a different content-type will receive
  a ``403 Forbidden`` from S3.
* **Restrict presigned URLs by IP** when possible using S3 bucket policy
  conditions (``aws:SourceIp``).
