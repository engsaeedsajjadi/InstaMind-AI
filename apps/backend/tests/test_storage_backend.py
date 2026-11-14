"""Object storage: magic-byte validation, keys, local backend."""

from __future__ import annotations

import pytest

from app.core.exceptions import UnsupportedMediaType, ValidationError
from app.modules.storage.backend import (
    LocalStorage,
    build_storage_key,
    detect_mime,
    get_storage,
    validate_upload,
)

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00" + b"\x00" * 16
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"


def test_detect_mime_sniffs_bytes_not_filenames():
    assert detect_mime(JPEG) == "image/jpeg"
    assert detect_mime(PNG) == "image/png"
    assert detect_mime(MP4) == "video/mp4"
    assert detect_mime(PDF) is None
    assert detect_mime(b"hello") is None


def test_validate_upload_accepts_real_media():
    mime, ext = validate_upload(data=JPEG, declared_filename="photo.png", kind="IMAGE")
    assert mime == "image/jpeg"
    assert ext == "jpg"
    mime, ext = validate_upload(data=PNG, declared_filename="photo.jpg", kind="IMAGE")
    assert mime == "image/png"
    mime, ext = validate_upload(data=MP4, declared_filename="clip.mov", kind="VIDEO")
    assert mime == "video/mp4"
    assert ext == "mp4"


def test_validate_upload_rejects_mismatched_and_empty_bytes():
    # A PDF renamed to .jpg is still a PDF.
    with pytest.raises(UnsupportedMediaType) as exc:
        validate_upload(data=PDF, declared_filename="photo.jpg", kind="IMAGE")
    assert exc.value.code == "unsupported_media_type"
    assert exc.value.status_code == 415
    # A JPEG is not a video.
    with pytest.raises(UnsupportedMediaType):
        validate_upload(data=JPEG, declared_filename="clip.mp4", kind="VIDEO")
    with pytest.raises(ValidationError):
        validate_upload(data=b"", declared_filename="empty.jpg", kind="IMAGE")


def test_build_storage_key_is_namespaced_and_safe():
    key = build_storage_key(kind="IMAGE", mime="image/jpeg", original_filename="../../etc/passwd.jpg")
    assert key.startswith("image/")
    assert ".." not in key
    assert key.endswith(".jpg")
    other = build_storage_key(kind="IMAGE", mime="image/jpeg", original_filename="photo.jpg")
    assert key != other  # uuid makes keys unique


@pytest.mark.asyncio
async def test_local_storage_round_trip(tmp_path):
    storage = LocalStorage(root=str(tmp_path), base_url="http://localhost:8000/media")
    stored = await storage.put("image/abc.jpg", JPEG, content_type="image/jpeg")
    assert stored.storage_backend == "local"
    assert stored.size_bytes == len(JPEG)
    assert stored.detected_mime == "image/jpeg"
    assert (await storage.public_url("image/abc.jpg")) == "http://localhost:8000/media/image/abc.jpg"
    await storage.delete("image/abc.jpg")
    await storage.delete("image/abc.jpg")  # deleting twice is fine
    assert not (tmp_path / "image" / "abc.jpg").exists()


@pytest.mark.asyncio
async def test_local_storage_rejects_path_traversal(tmp_path):
    storage = LocalStorage(root=str(tmp_path), base_url="http://localhost:8000/media")
    with pytest.raises(ValidationError):
        await storage.put("../escape.jpg", JPEG, content_type="image/jpeg")


def test_get_storage_defaults_to_local():
    assert isinstance(get_storage(), LocalStorage)
