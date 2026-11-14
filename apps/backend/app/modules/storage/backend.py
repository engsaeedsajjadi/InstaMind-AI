"""Object-storage backend.

Contract used by the rest of the codebase (routes, worker, agents):

* :func:`validate_upload` sniffs magic bytes — never trust the client's
  declared ``Content-Type`` or filename. Unknown or mismatched bytes are
  rejected with 415 before anything is stored.
* :func:`build_storage_key` produces a namespaced, collision-free key.
* :class:`StorageBackend` stores bytes and hands back a public URL Meta can
  fetch. ``put`` returns a :class:`StoredObject` with the fields the
  ``MediaAsset`` row needs (checksum, detected mime, size).
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.config import settings
from app.core.exceptions import ExternalServiceError, UnsupportedMediaType, ValidationError

# Magic-byte signatures. The declared filename/extension is *not* trusted:
# a ".jpg" that starts with ``%PDF`` is a PDF, not a JPEG.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # WEBP is RIFF....WEBP; checked precisely below
)

_IMAGE_MIMES = {"image/jpeg", "image/png"}
_VIDEO_MIMES = {"video/mp4", "video/quicktime"}

_EXTENSION_BY_MIME = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
}


def detect_mime(data: bytes) -> str | None:
    """Return the MIME type from magic bytes, or ``None`` if unknown."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    # MP4 / MOV: 4-byte size, then "ftyp" at offset 4.
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in {b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"dash"}:
            return "video/mp4"
        if brand in {b"qt  ", b"moov"}:
            return "video/quicktime"
        return "video/mp4"
    return None


def validate_upload(
    *,
    data: bytes,
    declared_filename: str = "",
    declared_content_type: str | None = None,
    kind: str = "IMAGE",
) -> tuple[str, str]:
    """Sniff ``data`` and return ``(detected_mime, extension)``.

    Raises :class:`UnsupportedMediaType` (415) when the bytes are not a media
    type this ``kind`` accepts, and :class:`ValidationError` when the upload
    is empty. The declared content type is only used as a hint in the error
    message — the magic bytes decide.
    """
    if not data:
        raise ValidationError("Uploaded file is empty.", context={"code": "empty_upload"})
    detected = detect_mime(data)
    allowed = _IMAGE_MIMES if kind.upper() == "IMAGE" else _VIDEO_MIMES
    if detected is None or detected not in allowed:
        raise UnsupportedMediaType(
            f"File type not accepted for {kind}: "
            f"detected {detected or 'unknown'}, declared {declared_content_type or declared_filename or 'none'}.",
        )
    return detected, _EXTENSION_BY_MIME[detected]


def build_storage_key(*, kind: str, mime: str, original_filename: str) -> str:
    """``<kind>/<uuid>-<sanitised-stem>.<ext>`` — no user input in the path."""
    ext = _EXTENSION_BY_MIME.get(mime) or mimetypes.guess_extension(mime, strict=False) or "bin"
    ext = ext.lstrip(".")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(original_filename).stem)[:40].strip("-") or "upload"
    prefix = "image" if kind.upper() == "IMAGE" else "video"
    return f"{prefix}/{uuid.uuid4().hex}-{stem}.{ext}"


@dataclass(frozen=True)
class StoredObject:
    storage_backend: str
    key: str
    bucket: str | None
    detected_mime: str
    size_bytes: int
    checksum_sha256: str


class StorageBackend(Protocol):
    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject: ...

    async def public_url(self, key: str, *, ttl_seconds: int = 3600) -> str: ...

    async def delete(self, key: str) -> None: ...


class LocalStorage:
    """Files on local disk, served under ``PUBLIC_MEDIA_BASE_URL``.

    This is the default backend and the one tests exercise. It is *not*
    suitable for multi-node production — use :class:`S3Storage` there.
    """

    def __init__(self, *, root: str | None = None, base_url: str | None = None) -> None:
        self.root = Path(root or settings.LOCAL_STORAGE_ROOT).expanduser()
        self.base_url = (base_url or settings.PUBLIC_MEDIA_BASE_URL).rstrip("/")
        self.root.mkdir(parents=True, exist_ok=True)

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return StoredObject(
            storage_backend="local",
            key=key,
            bucket=None,
            detected_mime=detect_mime(data) or content_type,
            size_bytes=len(data),
            checksum_sha256=hashlib.sha256(data).hexdigest(),
        )

    async def public_url(self, key: str, *, ttl_seconds: int = 3600) -> str:
        # Local files have no expiry; the ttl is accepted for interface parity.
        return f"{self.base_url}/{key}"

    async def delete(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)

    def _path(self, key: str) -> Path:
        # Never let a crafted key escape the storage root.
        resolved = (self.root / key).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ValidationError("Invalid storage key.", context={"code": "invalid_storage_key"})
        return resolved


class S3Storage:
    """S3/MinIO backend. ``boto3`` is an optional dependency: importing this
    class without it raises a loud, clear error instead of silently storing
    nothing."""

    def __init__(self, *, endpoint_url: str | None = None, bucket: str | None = None) -> None:
        try:
            import boto3  # noqa: PLC0415 - optional production dependency
        except ImportError as exc:
            raise ExternalServiceError(
                "S3 storage is configured but boto3 is not installed."
            ) from exc
        self._boto3 = boto3
        self.endpoint_url = endpoint_url or settings.S3_ENDPOINT_URL or None
        self.bucket = bucket or settings.S3_BUCKET
        self._client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            region_name=settings.S3_REGION,
            aws_access_key_id=settings.S3_ACCESS_KEY.get_secret_value() or None,
            aws_secret_access_key=settings.S3_SECRET_KEY.get_secret_value() or None,
        )

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject:
        self._client.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
        )
        return StoredObject(
            storage_backend="s3",
            key=key,
            bucket=self.bucket,
            detected_mime=detect_mime(data) or content_type,
            size_bytes=len(data),
            checksum_sha256=hashlib.sha256(data).hexdigest(),
        )

    async def public_url(self, key: str, *, ttl_seconds: int = 3600) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )

    async def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)


def get_storage() -> StorageBackend:
    """Backend selected by ``STORAGE_BACKEND``. ``local`` is the safe default."""
    backend = settings.STORAGE_BACKEND
    if backend == "s3":
        return S3Storage()
    if backend == "local":
        return LocalStorage()
    raise ExternalServiceError(f"Unknown storage backend: {backend!r}.")


__all__ = [
    "LocalStorage",
    "S3Storage",
    "StorageBackend",
    "StoredObject",
    "build_storage_key",
    "detect_mime",
    "get_storage",
    "validate_upload",
]
