granite\_assets.factory
=======================

The factory function inspects the configuration type and returns the correct
``IAssetRepository`` implementation.  It uses ``@overload`` signatures so that
type checkers (mypy, pyright) can infer the concrete return type when the
config type is known statically.

.. automodule:: granite_assets.factory
   :members:
   :undoc-members:
   :show-inheritance:
