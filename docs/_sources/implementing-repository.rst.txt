Implementing a Custom Repository
================================

Granite Assets uses a ``Protocol``-based contract (structural subtyping, PEP 544),
so you can implement a new backend without inheriting from any base class.
If your class provides all the required methods with compatible signatures, it
satisfies ``IAssetRepository`` — including at runtime, because the protocol is
decorated with ``@runtime_checkable``.

The Interface
-------------

.. code-block:: python

   from granite_assets import IAssetRepository

   # Runtime check (optional)
   assert isinstance(my_repo, IAssetRepository)

Methods you must implement:

+-----------------------------+--------------------------------------------------+
| Method                      | Description                                      |
+=============================+==================================================+
| ``save(request)``           | Persist an asset; return ``AssetSaveResult``.    |
+-----------------------------+--------------------------------------------------+
| ``delete(key)``             | Remove an asset; raise ``AssetNotFoundError``.   |
+-----------------------------+--------------------------------------------------+
| ``copy(src, dst, …)``       | Server-side copy without download.               |
+-----------------------------+--------------------------------------------------+
| ``move(src, dst, …)``       | Server-side move (copy + delete source).         |
+-----------------------------+--------------------------------------------------+
| ``exists(key)``             | Return ``True`` if the key exists.               |
+-----------------------------+--------------------------------------------------+
| ``get_descriptor(key)``     | Return ``AssetDescriptor`` without downloading.  |
+-----------------------------+--------------------------------------------------+
| ``build_public_url(key)``   | Return permanent public ``AssetAccessUrl``.      |
+-----------------------------+--------------------------------------------------+
| ``build_download_url(key)`` | Return time-limited ``AssetAccessUrl``.          |
+-----------------------------+--------------------------------------------------+
| ``build_upload_url(key, …)``| Return presigned ``UploadUrlResult``.            |
+-----------------------------+--------------------------------------------------+

Example: Azure Blob Storage Backend
--------------------------------------

Below is a minimal skeleton for an Azure Blob Storage backend.  It illustrates
the method signatures and exception mapping you need to implement.

.. code-block:: python

   from __future__ import annotations

   import hashlib
   import io
   from datetime import datetime, timezone, timedelta
   from typing import TYPE_CHECKING

   from granite_assets import (
       IAssetRepository,
       AssetSaveRequest,
       AssetSaveResult,
       AssetDescriptor,
       AssetAccessUrl,
       UploadUrlResult,
       AssetVisibility,
       AssetNotFoundError,
       AssetAccessNotSupportedError,
   )

   if TYPE_CHECKING:
       from azure.storage.blob import BlobServiceClient  # pip install azure-storage-blob

   class AzureBlobAssetRepositoryConfig:
       def __init__(
           self,
           connection_string: str,
           container: str,
           public_base_url: str | None = None,
           presign_ttl_seconds: int = 3600,
       ) -> None:
           self.connection_string = connection_string
           self.container = container
           self.public_base_url = public_base_url
           self.presign_ttl_seconds = presign_ttl_seconds


   class AzureBlobAssetRepository:
       """Azure Blob Storage implementation of IAssetRepository."""

       def __init__(self, config: AzureBlobAssetRepositoryConfig) -> None:
           from azure.storage.blob import BlobServiceClient
           self._client: BlobServiceClient = BlobServiceClient.from_connection_string(
               config.connection_string
           )
           self._container = config.container
           self._public_base_url = config.public_base_url
           self._presign_ttl = config.presign_ttl_seconds

       def _blob(self, key: str):
           return self._client.get_blob_client(container=self._container, blob=key)

       # ------------------------------------------------------------------
       # Write operations
       # ------------------------------------------------------------------

       def save(self, request: AssetSaveRequest) -> AssetSaveResult:
           source = request.open_source()
           data = source.read()
           blob = self._blob(request.key)
           blob.upload_blob(data, overwrite=request.overwrite or True)
           return AssetSaveResult(
               key=request.key,
               backend_ref=blob.url,
               content_length=len(data),
               checksum=hashlib.md5(data).hexdigest(),
               visibility=request.visibility or AssetVisibility.PRIVATE,
           )

       def delete(self, key: str) -> None:
           if not self.exists(key):
               raise AssetNotFoundError(key)
           self._blob(key).delete_blob()

       def copy(self, source_key: str, dest_key: str, *, overwrite: bool = True) -> None:
           if not self.exists(source_key):
               raise AssetNotFoundError(source_key)
           src_url = self._blob(source_key).url
           self._blob(dest_key).start_copy_from_url(src_url)

       def move(self, source_key: str, dest_key: str, *, overwrite: bool = True) -> None:
           self.copy(source_key, dest_key, overwrite=overwrite)
           self.delete(source_key)

       # ------------------------------------------------------------------
       # Query operations
       # ------------------------------------------------------------------

       def exists(self, key: str) -> bool:
           return self._blob(key).exists()

       def get_descriptor(self, key: str) -> AssetDescriptor:
           if not self.exists(key):
               raise AssetNotFoundError(key)
           props = self._blob(key).get_blob_properties()
           return AssetDescriptor(
               key=key,
               content_type=props.content_settings.content_type or "application/octet-stream",
               content_length=props.size,
               visibility=AssetVisibility.PUBLIC,  # inspect ACLs for a real impl
               last_modified=props.last_modified,
               checksum=props.etag,
               metadata=dict(props.metadata or {}),
           )

       # ------------------------------------------------------------------
       # URL construction
       # ------------------------------------------------------------------

       def build_public_url(self, key: str) -> AssetAccessUrl:
           if self._public_base_url:
               url = f"{self._public_base_url.rstrip('/')}/{key}"
           else:
               url = self._blob(key).url
           return AssetAccessUrl(url=url, expires_at=None)

       def build_download_url(
           self, key: str, ttl_seconds: int | None = None
       ) -> AssetAccessUrl:
           from azure.storage.blob import generate_blob_sas, BlobSasPermissions
           ttl = ttl_seconds or self._presign_ttl
           expiry = datetime.now(timezone.utc) + timedelta(seconds=ttl)
           # Real implementation: generate SAS token here
           raise NotImplementedError("generate SAS token and return AssetAccessUrl")

       def build_upload_url(
           self,
           key: str,
           content_type: str,
           ttl_seconds: int | None = None,
       ) -> UploadUrlResult:
           # Azure uses SAS tokens for presigned PUT equivalent
           raise NotImplementedError("generate SAS upload URL here")


   # ---------------------------------------------------------------------------
   # Register with the factory (optional)
   # ---------------------------------------------------------------------------
   # You can monkey-patch build_asset_repository or wire it yourself in DI:
   #
   #   from granite_assets import build_asset_repository
   #   # Use a factory wrapper instead:
   #
   #   def my_build_asset_repository(config):
   #       if isinstance(config, AzureBlobAssetRepositoryConfig):
   #           return AzureBlobAssetRepository(config)
   #       return build_asset_repository(config)

Implementation Checklist
------------------------

Use this checklist when building a new backend:

- [ ] Implement all 9 methods of ``IAssetRepository``.
- [ ] Raise ``AssetNotFoundError(key)`` when an asset is missing.
- [ ] Raise ``AssetAccessNotSupportedError(backend, operation)`` for unsupported
  URL features (e.g. presigned upload on a backend that does not support it).
- [ ] Raise ``AssetConfigurationError`` from ``__init__`` if the configuration is
  invalid (missing credentials, unreachable endpoint, etc.).
- [ ] ``save()`` must respect the ``overwrite`` field from ``AssetSaveRequest``
  (fall back to the config default if ``None``).
- [ ] ``move()`` should be atomic where the backend supports it; otherwise it is
  acceptable to copy then delete.
- [ ] Return ``AssetAccessUrl(url=..., expires_at=None)`` for permanent public
  URLs so that ``is_permanent`` returns ``True``.
- [ ] Add a configuration dataclass (preferably a ``@dataclass(slots=True)``)
  that holds all backend-specific settings.
- [ ] Write unit tests using mocks for the backend client.
- [ ] Verify ``isinstance(repo, IAssetRepository)`` passes at runtime.
