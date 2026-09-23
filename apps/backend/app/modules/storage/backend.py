"""Storage backends + upload sniffing.

Security notes
--------------
* ``validate_upload`` decides the type from **magic bytes**, never from the
  client-declared filename or ``Content-Type`` header — an ``evil.svg`` named
  ``photo.jpg`` is rejected before a single byte is persisted.
* ``build_storage_key`` never incorporates the original filename; keys are
  random UUIDs partitioned by kind/date, so path traversal through a hostile
  filename is impossible by construction.
* The local backend re-verifies every resolved path stays under its root.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

import httpx

from app.core.config import settings
from app.core.exceptions import ExternalServiceError, UnsupportedMediaType, ValidationError

# --------------------------------------------------------------------------- #
# Upload validation (magic-byte sniffing)
# --------------------------------------------------------------------------- #

_EXT_BY_MIME: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
}

_ALLOWED_BY_KIND: dict[str, frozenset[str]] = {
    "IMAGE": frozenset({"image/jpeg", "image/png"}),
    "VIDEO": frozenset({"video/mp4", "video/quicktime"}),
}


def _sniff_mime(data: bytes) -> str | None:
    """Detect the real content type from magic bytes, or ``None`` if unknown."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    # ISO Base Media File Format: [size(4)] 'ftyp' [major brand(4)] ...
    if len(data) >= 12 and data[4:8] == b"ftyp":
        major_brand = data[8:12]
        if major_brand == b"qt  ":
            return "video/quicktime"
        # isom / iso2 / mp41 / mp42 / M4V* / avc1 ... are all MP4 family.
        return "video/mp4"
    return None


def validate_upload(
    *,
    data: bytes,
    declared_filename: str,
    declared_content_type: str | None,
    kind: str,
) -> tuple[str, str]:
    """Return ``(detected_mime, extension)`` or raise.

    The declared filename/content-type are accepted for logging hints only —
    the decision is made purely on the byte content, and the result must be
    one of the types Instagram actually accepts for the requested ``kind``.
    """
    if not data:
        raise ValidationError("The uploaded file is empty.")

    detected = _sniff_mime(data)
    allowed = _ALLOWED_BY_KIND.get(kind, frozenset())
    if detected is None or detected not in allowed:
        raise UnsupportedMediaType(
            f"This file is not a valid {kind.lower()}. "
            f"Allowed: {', '.join(sorted(allowed))} (declared: "
            f"{declared_content_type or 'unknown'}, name: {declared_filename or 'unnamed'})."
        )
    return detected, _EXT_BY_MIME[detected]


def build_storage_key(*, kind: str, mime: str, original_filename: str) -> str:
    """Random, date-partitioned object key. The original filename is *not*
    embedded — it is untrusted input."""
    del original_filename  # intentional: see docstring
    now = datetime.now(UTC)
    ext = _EXT_BY_MIME.get(mime, ".bin")
    return f"{kind.lower()}/{now:%Y/%m}/{uuid.uuid4().hex}{ext}"


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StoredObject:
    """What a successful ``put`` returns — everything the ``media_assets`` row
    needs to persist."""

    storage_backend: str
    bucket: str | None
    key: str
    detected_mime: str
    size_bytes: int
    checksum_sha256: str


# --------------------------------------------------------------------------- #
# Local filesystem backend (dev/test)
# --------------------------------------------------------------------------- #


class LocalStorage:
    backend_name = "local"

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not str(path).startswith(str(self._root) + "/"):
            raise ValidationError("Invalid storage key.")
        return path

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        checksum = hashlib.sha256(data).hexdigest()
        await asyncio.to_thread(path.write_bytes, data)
        return StoredObject(
            storage_backend=self.backend_name,
            bucket=None,
            key=key,
            detected_mime=content_type,
            size_bytes=len(data),
            checksum_sha256=checksum,
        )

    async def public_url(self, key: str, *, ttl_seconds: int) -> str:
        # Local URLs are served by the app itself and do not expire; the ttl is
        # accepted to honour the interface (S3 presigning uses it).
        del ttl_seconds
        base = settings.PUBLIC_MEDIA_BASE_URL.rstrip("/")
        return f"{base}/{quote(key, safe='/')}"

    async def delete(self, key: str) -> None:
        path = self._resolve(key)
        if path.is_file():
            await asyncio.to_thread(path.unlink)

    def open(self, key: str) -> BinaryIO | None:
        path = self._resolve(key)
        return path.open("rb") if path.is_file() else None


# --------------------------------------------------------------------------- #
# S3-compatible backend (MinIO/AWS) — SigV4 over httpx, path-style
# --------------------------------------------------------------------------- #


def _hmac_sha256(key: bytes | str, msg: str) -> bytes:
    key_bytes = key if isinstance(key, bytes) else key.encode("utf-8")
    return hmac.new(key_bytes, msg.encode("utf-8"), hashlib.sha256).digest()


class S3Storage:
    """Minimal but real S3 client: PUT, DELETE, and presigned GET.

    Uses path-style addressing (``{endpoint}/{bucket}/{key}``) which is what
    MinIO and most self-hosted S3-compatible gateways require. For AWS proper,
    prefer a non-public bucket policy; the presign still works.
    """

    backend_name = "s3"
    _SERVICE = "s3"
    _ALGORITHM = "AWS4-HMAC-SHA256"
    _MAX_PRESIGN_TTL = 7 * 24 * 3600  # SigV4 ceiling

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str,
    ) -> None:
        if not endpoint:
            raise ExternalServiceError("S3 storage selected but S3_ENDPOINT_URL is not configured.")
        self._endpoint = endpoint.rstrip("/")
        self._bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region

    # -- low level -----------------------------------------------------------

    def _scope(self, date_stamp: str) -> str:
        return f"{date_stamp}/{self._region}/{self._SERVICE}/aws4_request"

    def _signing_key(self, date_stamp: str) -> bytes:
        k_date = _hmac_sha256(f"AWS4{self._secret_key}", date_stamp)
        k_region = _hmac_sha256(k_date, self._region)
        k_service = _hmac_sha256(k_region, self._SERVICE)
        return _hmac_sha256(k_service, "aws4_request")

    def _signature(
        self,
        *,
        method: str,
        canonical_uri: str,
        canonical_query: str,
        headers: dict[str, str],
        signed_headers: str,
        payload_hash: str,
        amz_date: str,
    ) -> str:
        canonical_request = "\n".join(
            (
                method,
                canonical_uri,
                canonical_query,
                "".join(f"{name}:{value}\n" for name, value in sorted(headers.items())),
                signed_headers,
                payload_hash,
            )
        )
        date_stamp = amz_date[:8]
        string_to_sign = "\n".join(
            (
                self._ALGORITHM,
                amz_date,
                self._scope(date_stamp),
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            )
        )
        return hmac.new(
            self._signing_key(date_stamp), string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _object_url(self, key: str) -> tuple[str, str]:
        canonical_uri = f"/{self._bucket}/{quote(key, safe='/~')}"
        return f"{self._endpoint}{canonical_uri}", canonical_uri

    # -- interface ------------------------------------------------------------

    async def _signed_request(self, method: str, key: str, data: bytes | None, content_type: str) -> httpx.Response:
        url, canonical_uri = self._object_url(key)
        now = datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        payload_hash = hashlib.sha256(data or b"").hexdigest()
        host = httpx.URL(url).host or ""
        if httpx.URL(url).port not in (None, 443 if url.startswith("https") else 80):
            host = f"{host}:{httpx.URL(url).port}"
        headers: dict[str, str] = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        signed_headers = ";".join(sorted(headers))
        signature = self._signature(
            method=method,
            canonical_uri=canonical_uri,
            canonical_query="",
            headers=headers,
            signed_headers=signed_headers,
            payload_hash=payload_hash,
            amz_date=amz_date,
        )
        date_stamp = amz_date[:8]
        authorization = (
            f"{self._ALGORITHM} Credential={self._access_key}/{self._scope(date_stamp)}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        request_headers = {
            "Authorization": authorization,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if content_type:
            request_headers["Content-Type"] = content_type
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                return await client.request(method, url, content=data, headers=request_headers)
        except httpx.HTTPError as exc:
            raise ExternalServiceError(f"Object storage is unreachable: {exc!s}",) from exc

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject:
        response = await self._signed_request("PUT", key, data, content_type)
        if response.status_code not in (200, 201):
            raise ExternalServiceError(
                f"Object storage rejected the upload (HTTP {response.status_code}).",
            )
        return StoredObject(
            storage_backend=self.backend_name,
            bucket=self._bucket,
            key=key,
            detected_mime=content_type,
            size_bytes=len(data),
            checksum_sha256=hashlib.sha256(data).hexdigest(),
        )

    async def public_url(self, key: str, *, ttl_seconds: int) -> str:
        """SigV4 presigned GET — Meta's crawlers fetch media from this URL."""
        _, canonical_uri = self._object_url(key)
        base_url, _ = self._object_url(key)
        now = datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = amz_date[:8]
        credential = f"{self._access_key}/{self._scope(date_stamp)}"
        ttl = min(ttl_seconds, self._MAX_PRESIGN_TTL)
        host = httpx.URL(base_url).netloc.decode()
        params = {
            "X-Amz-Algorithm": self._ALGORITHM,
            "X-Amz-Credential": credential,
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(ttl),
            "X-Amz-SignedHeaders": "host",
        }
        canonical_query = "&".join(f"{k}={quote(v, safe='')}" for k, v in sorted(params.items()))
        signature = self._signature(
            method="GET",
            canonical_uri=canonical_uri,
            canonical_query=canonical_query,
            headers={"host": host},
            signed_headers="host",
            payload_hash="UNSIGNED-PAYLOAD",
            amz_date=amz_date,
        )
        return f"{base_url}?{canonical_query}&X-Amz-Signature={signature}"

    async def delete(self, key: str) -> None:
        response = await self._signed_request("DELETE", key, None, "")
        # 204 = deleted, 404 = already gone; both leave the system consistent.
        if response.status_code not in (204, 404):
            raise ExternalServiceError(
                f"Object storage rejected the delete (HTTP {response.status_code}).",
            )


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #

_storage: LocalStorage | S3Storage | None = None


def get_storage() -> LocalStorage | S3Storage:
    """Process-wide storage client, built from settings."""
    global _storage
    if _storage is None:
        if settings.STORAGE_BACKEND == "local":
            _storage = LocalStorage(Path(settings.LOCAL_STORAGE_ROOT))
        else:
            _storage = S3Storage(
                endpoint=settings.S3_ENDPOINT_URL,
                bucket=settings.S3_BUCKET,
                access_key=settings.S3_ACCESS_KEY.get_secret_value(),
                secret_key=settings.S3_SECRET_KEY.get_secret_value(),
                region=settings.S3_REGION,
            )
    return _storage


def reset_storage() -> None:
    """Drop the cached client (tests swap settings between cases)."""
    global _storage
    _storage = None
