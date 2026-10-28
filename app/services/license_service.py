"""Issue, revoke, transfer and extend one-time (local edition) licenses."""

from __future__ import annotations

import base64
import secrets
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.licensing import keys
from app.models.platform import License, PlatformAdmin
from app.schemas.platform import LicenseIssue
from app.services.platform_admin_service import record_audit

PAGE_SIZE_MAX = 100


def _private_key() -> str:
    if not settings.license_signing_private_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="License signing is not configured (LICENSE_SIGNING_PRIVATE_KEY)",
        )
    return settings.license_signing_private_key


def _new_license_id() -> str:
    return "LIC-" + base64.b32encode(secrets.token_bytes(8)).decode().rstrip("=")


def _sign(license_row: License) -> str:
    payload = keys.build_payload(
        license_id=license_row.license_id,
        customer=license_row.customer_name,
        machine_id=license_row.machine_id,
        issued_at=license_row.issued_at,
        updates_until=license_row.updates_until,
        edition=license_row.edition,
    )
    return keys.sign_license(payload, _private_key())


def _snapshot(lic: License) -> dict[str, Any]:
    return {
        "license_id": lic.license_id,
        "customer": lic.customer_name,
        "machine_id": lic.machine_id,
        "updates_until": lic.updates_until,
        "status": lic.status,
        "transfer_count": lic.transfer_count,
    }


async def _get(db: AsyncSession, license_id: UUID, *, lock: bool = False) -> License:
    stmt = select(License).where(License.id == license_id)
    if lock:
        stmt = stmt.with_for_update()
    lic = (await db.execute(stmt)).scalar_one_or_none()
    if lic is None:
        raise HTTPException(status_code=404, detail="License not found")
    return lic


async def get_license(db: AsyncSession, license_id: UUID) -> License:
    return await _get(db, license_id)


async def issue_license(
    db: AsyncSession, admin: PlatformAdmin, data: LicenseIssue, ip: str | None
) -> License:
    _private_key()  # fail early, before any row exists
    if data.updates_until < date.today():
        raise HTTPException(status_code=400, detail="updates_until is in the past")
    lic = License(
        license_id=_new_license_id(),
        customer_name=data.customer_name.strip(),
        phone=data.phone,
        email=data.email,
        edition=keys.EDITION_LOCAL,
        machine_id=data.machine_id.strip(),
        issued_at=datetime.now(timezone.utc).replace(microsecond=0),
        updates_until=data.updates_until,
        status="issued",
        transfer_count=0,
        notes=data.notes,
        key_string="",
        created_by=admin.id,
    )
    lic.key_string = _sign(lic)
    db.add(lic)
    try:
        await db.flush()
    except IntegrityError:  # license_id collision: astronomically unlikely, but safe
        await db.rollback()
        raise HTTPException(status_code=409, detail="Could not allocate a license id; retry") from None
    await record_audit(
        db, admin, action="license.issue", entity_type="license", entity_id=lic.id,
        entity_label=f"{lic.customer_name} ({lic.license_id})", after=_snapshot(lic), ip_address=ip,
    )
    await db.commit()
    await db.refresh(lic)
    return lic


async def list_licenses(
    db: AsyncSession, *, search: str | None, status_filter: str | None, page: int, page_size: int
) -> dict[str, Any]:
    page_size = max(1, min(page_size, PAGE_SIZE_MAX))
    page = max(1, page)
    filters = []
    if search and search.strip():
        like = f"%{search.strip()}%"
        filters.append(
            or_(
                License.customer_name.ilike(like),
                License.phone.ilike(like),
                License.email.ilike(like),
                License.machine_id.ilike(like),
                License.license_id.ilike(like),
            )
        )
    if status_filter:
        filters.append(License.status == status_filter)
    total = (await db.execute(select(func.count()).select_from(License).where(*filters))).scalar_one()
    rows = (
        await db.execute(
            select(License)
            .where(*filters)
            .order_by(License.created_at.desc(), License.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return {"items": list(rows), "total": total, "page": page, "page_size": page_size}


async def revoke_license(
    db: AsyncSession, admin: PlatformAdmin, license_id: UUID, ip: str | None
) -> License:
    lic = await _get(db, license_id, lock=True)
    if lic.status == "revoked":
        raise HTTPException(status_code=409, detail="License is already revoked")
    before = _snapshot(lic)
    lic.status = "revoked"
    lic.revoked_at = datetime.now(timezone.utc)
    await record_audit(
        db, admin, action="license.revoke", entity_type="license", entity_id=lic.id,
        entity_label=f"{lic.customer_name} ({lic.license_id})", before=before,
        after=_snapshot(lic), ip_address=ip,
    )
    await db.commit()
    await db.refresh(lic)
    return lic


async def transfer_license(
    db: AsyncSession,
    admin: PlatformAdmin,
    license_id: UUID,
    machine_id: str,
    notes: str | None,
    ip: str | None,
) -> tuple[License, License]:
    """Move a license to a new machine: the old one is revoked, a new key is issued."""
    _private_key()
    old = await _get(db, license_id, lock=True)
    if old.status == "revoked":
        raise HTTPException(status_code=409, detail="A revoked license cannot be transferred")
    new_machine = machine_id.strip()
    if keys.normalize_machine_id(new_machine) == keys.normalize_machine_id(old.machine_id):
        raise HTTPException(status_code=400, detail="That is already the licensed machine")

    before = _snapshot(old)
    now = datetime.now(timezone.utc)
    old.status = "revoked"
    old.revoked_at = now

    new = License(
        license_id=_new_license_id(),
        customer_name=old.customer_name,
        phone=old.phone,
        email=old.email,
        edition=old.edition,
        machine_id=new_machine,
        issued_at=now.replace(microsecond=0),
        updates_until=old.updates_until,
        status="issued",
        transfer_count=old.transfer_count + 1,
        previous_license_id=old.id,
        notes=notes or old.notes,
        key_string="",
        created_by=admin.id,
    )
    new.key_string = _sign(new)
    db.add(new)
    await db.flush()
    await record_audit(
        db, admin, action="license.transfer", entity_type="license", entity_id=new.id,
        entity_label=f"{new.customer_name} ({new.license_id})",
        before=before,
        after={**_snapshot(new), "replaces": old.license_id},
        ip_address=ip,
    )
    await db.commit()
    await db.refresh(old)
    await db.refresh(new)
    return old, new


async def extend_updates(
    db: AsyncSession, admin: PlatformAdmin, license_id: UUID, updates_until: date, ip: str | None
) -> License:
    lic = await _get(db, license_id, lock=True)
    if lic.status == "revoked":
        raise HTTPException(status_code=409, detail="A revoked license cannot be extended")
    if updates_until <= lic.updates_until:
        raise HTTPException(status_code=400, detail="New date must be later than the current one")
    before = _snapshot(lic)
    lic.updates_until = updates_until
    lic.key_string = _sign(lic)  # same license_id and machine, longer update window
    await record_audit(
        db, admin, action="license.extend_updates", entity_type="license", entity_id=lic.id,
        entity_label=f"{lic.customer_name} ({lic.license_id})", before=before,
        after=_snapshot(lic), ip_address=ip,
    )
    await db.commit()
    await db.refresh(lic)
    return lic


def license_file_text(lic: License) -> str:
    return (
        "PakPOS license\n"
        "==============\n"
        f"Customer      : {lic.customer_name}\n"
        f"License ID    : {lic.license_id}\n"
        f"Machine ID    : {lic.machine_id}\n"
        f"Updates until : {lic.updates_until.isoformat()}\n"
        "\n"
        "Paste the line below into the app (Settings > License):\n"
        "\n"
        f"{lic.key_string}\n"
    )


async def signed_revocation_list(db: AsyncSession) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(License.license_id, License.revoked_at).where(License.status == "revoked")
        )
    ).all()
    document = keys.build_revocation_document(
        [(license_id, at or datetime.now(timezone.utc)) for license_id, at in rows],
        datetime.now(timezone.utc),
    )
    document["signature"] = keys.sign_document(document, _private_key())
    return document
