"""Public receipt links.

A link is ``<PUBLIC_BASE_URL>/r/<token>`` where ``token = <id>.<sig>``: ``id`` is a random
UUID (url-safe base64) naming a ``receipt_links`` row, ``sig`` an HMAC of it. The token holds
no business, sale or customer data. The row decides everything else: which sale, when it
expires (``RECEIPT_LINK_DAYS``) and whether it was revoked. A forged id fails the HMAC before
the database is touched.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.receipt_link import ReceiptLink
from app.models.sales import Sale


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _sign(link_id: UUID) -> str:
    key = settings.jwt_secret_key.encode()
    digest = hmac.new(key, b"receipt-link:" + link_id.bytes, hashlib.sha256).digest()
    return _b64(digest[:16])


def make_token(link_id: UUID) -> str:
    return f"{_b64(link_id.bytes)}.{_sign(link_id)}"


def parse_token(token: str) -> UUID | None:
    """The link id when the signature is valid, else None (never raises)."""
    try:
        id_part, sig = token.split(".", 1)
        raw = base64.urlsafe_b64decode(id_part + "=" * (-len(id_part) % 4))
        link_id = UUID(bytes=raw)
    except Exception:  # noqa: BLE001 - any malformed token is simply invalid
        return None
    if not hmac.compare_digest(_sign(link_id), sig):
        return None
    return link_id


def build_url(token: str, request_base_url: str | None = None) -> str:
    base = (settings.public_base_url or request_base_url or "").rstrip("/")
    return f"{base}/r/{token}"


@dataclass
class IssuedLink:
    url: str
    expires_at: datetime
    token: str


async def _load_sale(db: AsyncSession, business_id: UUID, sale_id: UUID) -> Sale:
    sale = (
        await db.execute(
            select(Sale).where(
                Sale.id == sale_id,
                Sale.business_id == business_id,
                Sale.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if sale is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sale not found")
    return sale


async def find_sale_by_number(
    db: AsyncSession,
    business_id: UUID,
    *,
    sale_number: str | None,
    client_receipt_no: str | None,
) -> Sale:
    """Resolve a sale by its official number or the receipt number printed offline."""
    if not sale_number and not client_receipt_no:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "sale_number or client_receipt_no is required",
        )
    stmt = select(Sale).where(Sale.business_id == business_id, Sale.deleted_at.is_(None))
    if sale_number:
        stmt = stmt.where(Sale.sale_number == sale_number)
    if client_receipt_no:
        stmt = stmt.where(Sale.client_receipt_no == client_receipt_no)
    sale = (await db.execute(stmt)).scalars().first()
    if sale is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sale not found")
    return sale


async def issue_link(
    db: AsyncSession,
    business_id: UUID,
    sale: Sale,
    created_by: UUID,
    request_base_url: str | None,
) -> IssuedLink:
    now = datetime.now(timezone.utc)
    link = ReceiptLink(
        id=uuid.uuid4(),
        business_id=business_id,
        sale_id=sale.id,
        created_by=created_by,
        created_at=now,
        expires_at=now + timedelta(days=settings.receipt_link_days),
    )
    db.add(link)
    await db.commit()
    token = make_token(link.id)
    return IssuedLink(build_url(token, request_base_url), link.expires_at, token)


async def issue_for_sale_id(
    db: AsyncSession,
    business_id: UUID,
    sale_id: UUID,
    created_by: UUID,
    request_base_url: str | None,
) -> IssuedLink:
    sale = await _load_sale(db, business_id, sale_id)
    return await issue_link(db, business_id, sale, created_by, request_base_url)


async def revoke_links_for_sale(
    db: AsyncSession, business_id: UUID, sale_id: UUID, revoked_by: UUID
) -> int:
    await _load_sale(db, business_id, sale_id)
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(ReceiptLink)
        .where(
            ReceiptLink.business_id == business_id,
            ReceiptLink.sale_id == sale_id,
            ReceiptLink.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoked_by=revoked_by)
    )
    await db.commit()
    return result.rowcount or 0


class LinkProblem(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message


async def resolve_link(db: AsyncSession, token: str) -> tuple[ReceiptLink, UUID]:
    """(link, sale_id) for a usable token, else LinkProblem(404 | 410)."""
    link_id = parse_token(token)
    if link_id is None:
        raise LinkProblem(404, "This receipt link is not valid.")
    link = await db.get(ReceiptLink, link_id)
    if link is None or link.revoked_at is not None:
        raise LinkProblem(404, "This receipt link is not valid.")
    if link.expires_at <= datetime.now(timezone.utc):
        raise LinkProblem(410, "This receipt link has expired.")
    return link, link.sale_id
