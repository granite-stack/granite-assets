Quick Start
===========

This page shows the two most common setups — local filesystem and AWS S3 — so you
can be productive in minutes.

Local Filesystem (Nginx)
------------------------

Good for development, single-server deployments, or any setup where a static web
server (Nginx, Apache, Caddy …) serves files directly from disk.

.. code-block:: python

   from granite_assets import (
       LocalNginxAssetRepositoryConfig,
       LocalNginxAssetRepository,
       AssetSaveRequest,
       AssetVisibility,
   )

   # 1. Configure the repository.
   config = LocalNginxAssetRepositoryConfig(
       storage_path="/var/www/assets",      # absolute path on disk
       base_url="https://cdn.example.com/assets",  # how Nginx serves it
       public_prefix="public",             # default; public files go here
       private_prefix="private",           # private files (Nginx-protected)
   )
   repo = LocalNginxAssetRepository(config)

   # 2. Save an asset — explicit key.
   with open("photo.jpg", "rb") as f:
       result = repo.save(AssetSaveRequest(
           key="avatars/user-42.jpg",
           source=f,
           content_type="image/jpeg",
           visibility=AssetVisibility.PUBLIC,
           filename="photo.jpg",
       ))
   print(result.key)            # avatars/user-42.jpg
   print(result.content_length) # bytes written

   # Auto-generated key — omit ``key``; granite-assets creates
   # ``{uuid}/{uuid}.jpg`` automatically.
   with open("photo.jpg", "rb") as f:
       result = repo.save(AssetSaveRequest(
           source=f,
           content_type="image/jpeg",
           visibility=AssetVisibility.PUBLIC,
           filename="photo.jpg",
       ))
   print(result.key)  # e.g. "a1b2c3d4-.../<same-uuid>.jpg"

   # 3. Build a public URL.
   url = repo.build_public_url("avatars/user-42.jpg")
   print(url.url)   # https://cdn.example.com/assets/public/avatars/user-42.jpg
   print(url.is_permanent)  # True

   # 4. Check existence.
   assert repo.exists("avatars/user-42.jpg")

   # 5. Get metadata (no download).
   descriptor = repo.get_descriptor("avatars/user-42.jpg")
   print(descriptor.content_length)
   print(descriptor.last_modified)

   # 6. Copy and move.
   repo.copy("avatars/user-42.jpg", "avatars/backup/user-42.jpg")
   repo.move("avatars/backup/user-42.jpg", "archive/user-42.jpg")

   # 7. Delete.
   repo.delete("avatars/user-42.jpg")

AWS S3
------

Replace the configuration object; the API is identical.

.. code-block:: python

   from granite_assets import (
       S3AssetRepositoryConfig,
       AssetSaveRequest,
       AssetVisibility,
       build_asset_repository,
   )

   config = S3AssetRepositoryConfig(
       bucket="my-app-assets",
       region="eu-west-1",
       key_prefix="production/",           # optional prefix inside the bucket
       public_base_url="https://cdn.example.com",  # CDN in front of the bucket
       presign_ttl_seconds=3600,           # default TTL for signed URLs
   )
   repo = build_asset_repository(config)   # returns S3AssetRepository

   # Save a public asset — explicit key.
   with open("banner.png", "rb") as f:
       result = repo.save(AssetSaveRequest(
           key="banners/homepage.png",
           source=f,
           content_type="image/png",
           visibility=AssetVisibility.PUBLIC,
       ))

   # Save with auto-generated key (recommended for user uploads).
   with open("invoice.pdf", "rb") as f:
       result = repo.save(AssetSaveRequest(
           source=f,
           content_type="application/pdf",
           visibility=AssetVisibility.PRIVATE,
           filename="invoice.pdf",
       ))
   print(result.key)
   # e.g. "3b105bc5-6056-4a52-b03b-7d953644c826/3b105bc5-....pdf"

   # Permanent public URL (via CDN).
   url = repo.build_public_url("banners/homepage.png")
   print(url.url)   # https://cdn.example.com/banners/homepage.png

   # Save a private asset.
   with open("invoice.pdf", "rb") as f:
       repo.save(AssetSaveRequest(
           key="invoices/inv-001.pdf",
           source=f,
           content_type="application/pdf",
           visibility=AssetVisibility.PRIVATE,
       ))

   # Time-limited download URL.
   dl = repo.build_download_url("invoices/inv-001.pdf", ttl_seconds=300)
   print(dl.url)        # https://... (presigned S3 URL)
   print(dl.expires_at) # UTC datetime 5 minutes from now

Using the Factory
-----------------

``build_asset_repository`` inspects the config type and returns the right
repository.  This is useful in dependency-injection setups:

.. code-block:: python

   from granite_assets import build_asset_repository, LocalNginxAssetRepositoryConfig

   def get_repo():
       # In production, read from environment / settings
       config = LocalNginxAssetRepositoryConfig(
           storage_path="/var/www/assets",
           base_url="http://localhost/assets",
       )
       return build_asset_repository(config)

Error Handling
--------------

.. code-block:: python

   from granite_assets import (
       AssetError,
       AssetNotFoundError,
       AssetAccessNotSupportedError,
   )

   try:
       repo.delete("missing-key.jpg")
   except AssetNotFoundError as e:
       print(f"Key not found: {e.key}")

   try:
       repo.build_upload_url("some/key.jpg", "image/jpeg")
   except AssetAccessNotSupportedError as e:
       print(f"{e.backend}: {e.operation} not supported")

   try:
       repo.save(bad_request)
   except AssetError as e:
       # Base class for all granite-assets errors
       print(e)
