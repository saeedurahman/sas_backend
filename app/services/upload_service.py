"""Image uploads: Cloudinary when CLOUDINARY_URL is set, local /static in development."""

from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import UUID

import httpx
from fastapi import HTTPException, UploadFile, status

from app.core.config import settings

_CHUNK = 64 * 1024


def sniff_image(head: bytes) -> tuple[str, str] | None:
    """(extension, mime) from the file's magic bytes; the client's content type is not trusted."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


async def read_limited(file: UploadFile, limit: int) -> bytes:
    data = bytearray()
    while chunk := await file.read(_CHUNK):
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"Image too large: the limit is {limit // 1024} KB",
            )
    return bytes(data)


def cloudinary_credentials() -> tuple[str, str, str] | None:
    """(api_key, api_secret, cloud_name) from ``cloudinary://key:secret@cloud``."""
    raw = settings.cloudinary_url.strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme != "cloudinary" or not (parsed.username and parsed.password and parsed.hostname):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "CLOUDINARY_URL is malformed on the server"
        )
    return unquote(parsed.username), unquote(parsed.password), parsed.hostname


async def _upload_cloudinary(data: bytes, ext: str, creds: tuple[str, str, str], business_id: UUID) -> str:
    api_key, api_secret, cloud = creds
    timestamp = str(int(time.time()))
    folder = f"{settings.cloudinary_folder.strip('/')}/{business_id}"
    to_sign = f"folder={folder}&timestamp={timestamp}{api_secret}"
    signature = hashlib.sha1(to_sign.encode()).hexdigest()  # noqa: S324 - Cloudinary's scheme
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"https://api.cloudinary.com/v1_1/{cloud}/image/upload",
                data={
                    "api_key": api_key,
                    "timestamp": timestamp,
                    "folder": folder,
                    "signature": signature,
                },
                files={"file": (f"{uuid.uuid4().hex}.{ext}", data)},
            )
    except httpx.HTTPError:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Image storage is unreachable") from None
    if response.status_code != 200:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Image storage rejected the upload")
    url = response.json().get("secure_url")
    if not url:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Image storage returned no URL")
    return url


def _save_local(data: bytes, ext: str, business_id: UUID, base_url: str) -> str:
    folder = Path(settings.upload_local_dir) / str(business_id)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{ext}"
    (folder / name).write_bytes(data)
    base = (settings.public_base_url or base_url).rstrip("/")
    return f"{base}/static/uploads/{business_id}/{name}"


async def store_image(file: UploadFile, business_id: UUID, request_base_url: str) -> str:
    creds = cloudinary_credentials()
    if creds is None and settings.is_production:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Image uploads are not configured: set CLOUDINARY_URL",
        )
    data = await read_limited(file, settings.upload_max_image_bytes)
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The file is empty")
    kind = sniff_image(data[:16])
    if kind is None:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Only JPEG, PNG, WebP or GIF images are accepted"
        )
    ext, _mime = kind
    if creds is not None:
        return await _upload_cloudinary(data, ext, creds, business_id)
    return _save_local(data, ext, business_id, request_base_url)
