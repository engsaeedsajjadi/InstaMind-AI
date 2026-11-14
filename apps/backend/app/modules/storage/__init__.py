"""Object storage.

The API never talks to a disk or to S3 directly: media bytes go through the
:class:`StorageBackend` protocol, and Meta fetches published media from the
public URL the backend hands back. ``LocalStorage`` is the default (and the
test) backend; ``S3Storage`` is the production backend and fails loudly when
``boto3`` is not installed instead of silently pretending to store anything.
"""

from __future__ import annotations

from app.modules.storage.backend import (
    LocalStorage,
    S3Storage,
    StorageBackend,
    StoredObject,
    build_storage_key,
    get_storage,
    validate_upload,
)

__all__ = [
    "LocalStorage",
    "S3Storage",
    "StorageBackend",
    "StoredObject",
    "build_storage_key",
    "get_storage",
    "validate_upload",
]
