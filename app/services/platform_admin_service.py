"""Platform admin accounts: sign-in (password + TOTP), lockout, management, audit."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import uuid
from app.core.config import settings
from app.core.platform_security import (
    create_platform_token,
    decrypt_totp_secret,
    encrypt_totp_secret,
    generate_recovery_codes,
    generate_totp_secret,
    hash_recovery_code,
    provisioning_uri,
    verify_and_consume_recovery_code,
    verify_totp,
)
from app.database import AsyncSessionLocal
from app.models.platform import PlatformAdmin, PlatformAuditLog, PlatformRevokedToken
from app.services.auth_service import hash_password, verify_password

_GENERIC = "Invalid email, password or code"
# A real bcrypt hash so unknown e-mails cost the same as known ones.
_DUMMY_HASH = hash_password("dummy-password-for-timing")


def _invalid() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_GENERIC)


def normalize_email(email: str) -> str:
    return email.strip().lower()


# ── audit ─────────────────────────────────────────────────────────────────────


async def record_audit(
    db: AsyncSession,
    admin: PlatformAdmin | None,
    *,
    action: str,
    entity_type: str,
    entity_id: Any = None,
    entity_label: str | None = None,
    business_id: UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip_address: str | None = None,
    admin_email: str | None = None,
) -> None:
    """Add an audit row to the caller's transaction (it commits with the mutation)."""
    db.add(
        PlatformAuditLog(
            admin_id=admin.id if admin is not None else None,
            admin_email=admin.email if admin is not None else (admin_email or "unknown"),
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            entity_label=entity_label,
            business_id=business_id,
            before=_jsonable(before),
            after=_jsonable(after),
            ip_address=ip_address,
        )
    )


def _jsonable(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    out: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, (datetime, date)):
            out[key] = item.isoformat()
        elif isinstance(item, (UUID,)):
            out[key] = str(item)
        elif hasattr(item, "quantize"):  # Decimal
            out[key] = str(item)
        else:
            out[key] = item
    return out


# ── sign-in ───────────────────────────────────────────────────────────────────


async def _record_failure(
    admin_id: UUID, admin_email: str, ip_address: str | None, reason: str
) -> None:
    """Persist a failed attempt (and the lock) in its own committed transaction."""
    now = datetime.now(timezone.utc)
    lock_until = now + timedelta(minutes=settings.platform_lockout_minutes)
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    UPDATE platform_admins
                       SET failed_login_attempts = 0, locked_until = NULL
                     WHERE id = :id AND locked_until IS NOT NULL AND locked_until <= :now
                    """
                ),
                {"id": admin_id, "now": now},
            )
            attempts = (
                await session.execute(
                    text(
                        "UPDATE platform_admins SET failed_login_attempts = failed_login_attempts + 1 "
                        "WHERE id = :id RETURNING failed_login_attempts"
                    ),
                    {"id": admin_id},
                )
            ).scalar_one()
            locked = attempts >= settings.platform_max_login_attempts
            if locked:
                await session.execute(
                    text("UPDATE platform_admins SET locked_until = :until WHERE id = :id"),
                    {"id": admin_id, "until": lock_until},
                )
            session.add(
                PlatformAuditLog(
                    admin_id=admin_id,
                    admin_email=admin_email,
                    action="auth.login_failed" if not locked else "auth.locked",
                    entity_type="admin",
                    entity_id=str(admin_id),
                    entity_label=admin_email,
                    after={"reason": reason, "attempts": attempts},
                    ip_address=ip_address,
                )
            )


async def login(
    db: AsyncSession,
    email: str,
    password: str,
    totp_code: str | None,
    ip_address: str | None,
    recovery_code: str | None = None,
) -> tuple[PlatformAdmin, str, int]:
    email = normalize_email(email)
    admin = (
        await db.execute(select(PlatformAdmin).where(PlatformAdmin.email == email))
    ).scalar_one_or_none()

    if admin is None or not admin.is_active:
        await asyncio.to_thread(verify_password, password, _DUMMY_HASH)
        raise _invalid()

    now = datetime.now(timezone.utc)
    if admin.locked_until is not None and admin.locked_until > now:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account locked after too many attempts. Try again later.",
            headers={
                "Retry-After": str(int((admin.locked_until - now).total_seconds()) + 1)
            },
        )

    if not await asyncio.to_thread(verify_password, password, admin.password_hash):
        await _record_failure(admin.id, admin.email, ip_address, "password")
        raise _invalid()

    if recovery_code:
        if not verify_and_consume_recovery_code(admin, recovery_code):
            await _record_failure(admin.id, admin.email, ip_address, "recovery_code")
            raise _invalid()
        action = "auth.recovery_code_used"
    else:
        plain_secret = decrypt_totp_secret(admin.totp_secret)
        step = verify_totp(plain_secret, totp_code or "")
        if step is None:
            await _record_failure(admin.id, admin.email, ip_address, "totp")
            raise _invalid()

        # A code works once: only a strictly newer time-step may be recorded.
        accepted = (
            await db.execute(
                text(
                    "UPDATE platform_admins SET last_totp_step = :step "
                    "WHERE id = :id AND last_totp_step < :step RETURNING id"
                ),
                {"step": step, "id": admin.id},
            )
        ).first()
        if accepted is None:
            await _record_failure(admin.id, admin.email, ip_address, "totp_replay")
            raise _invalid()
        action = "auth.login"

    await db.execute(
        update(PlatformAdmin)
        .where(PlatformAdmin.id == admin.id)
        .values(
            failed_login_attempts=0,
            locked_until=None,
            last_login=now,
            recovery_codes_hashes=admin.recovery_codes_hashes,
        )
    )
    await record_audit(
        db,
        admin,
        action=action,
        entity_type="admin",
        entity_id=admin.id,
        entity_label=admin.email,
        ip_address=ip_address,
    )
    await db.commit()
    await db.refresh(admin)
    token, expires_in = create_platform_token(admin.id, admin.token_version)
    return admin, token, expires_in


# ── management ────────────────────────────────────────────────────────────────


def credentials_for(admin: PlatformAdmin) -> dict[str, Any]:
    plain_secret = getattr(admin, "_plain_totp_secret", None)
    if plain_secret is None:
        plain_secret = decrypt_totp_secret(admin.totp_secret)
    recovery_codes = getattr(admin, "_plain_recovery_codes", None)
    res: dict[str, Any] = {
        "totp_secret": plain_secret,
        "provisioning_uri": provisioning_uri(admin.email, plain_secret),
    }
    if recovery_codes is not None:
        res["recovery_codes"] = recovery_codes
    return res


async def list_admins(db: AsyncSession) -> list[PlatformAdmin]:
    return list(
        (await db.execute(select(PlatformAdmin).order_by(PlatformAdmin.created_at))).scalars()
    )


async def create_admin(
    db: AsyncSession,
    actor: PlatformAdmin | None,
    *,
    email: str,
    name: str,
    password: str,
    ip_address: str | None = None,
    generate_recovery_codes_flag: bool = False,
) -> PlatformAdmin:
    admin_id = uuid.uuid4()
    plain_secret = generate_totp_secret()
    enc_secret = encrypt_totp_secret(plain_secret)
    codes = generate_recovery_codes(10) if generate_recovery_codes_flag else None
    admin = PlatformAdmin(
        id=admin_id,
        email=normalize_email(email),
        name=name.strip(),
        password_hash=await asyncio.to_thread(hash_password, password),
        totp_secret=enc_secret,
        recovery_codes_hashes=[hash_recovery_code(admin_id, c) for c in codes] if codes else None,
        is_active=True,
    )
    admin._plain_totp_secret = plain_secret
    admin._plain_recovery_codes = codes
    db.add(admin)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="An admin with this email already exists"
        ) from None
    await record_audit(
        db,
        actor,
        action="admin.create",
        entity_type="admin",
        entity_id=admin.id,
        entity_label=admin.email,
        after={"email": admin.email, "name": admin.name},
        ip_address=ip_address,
    )
    await db.commit()
    await db.refresh(admin)
    admin._plain_totp_secret = plain_secret
    admin._plain_recovery_codes = codes
    return admin


async def _get_admin(db: AsyncSession, admin_id: UUID) -> PlatformAdmin:
    admin = await db.get(PlatformAdmin, admin_id)
    if admin is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin not found")
    return admin


async def set_admin_active(
    db: AsyncSession,
    actor: PlatformAdmin,
    admin_id: UUID,
    active: bool,
    ip_address: str | None = None,
) -> PlatformAdmin:
    admin = await _get_admin(db, admin_id)
    if admin.id == actor.id and not active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot disable your own account"
        )
    before = {"is_active": admin.is_active}
    admin.is_active = active
    if not active:
        admin.token_version += 1  # signs them out everywhere
    await record_audit(
        db,
        actor,
        action="admin.enable" if active else "admin.disable",
        entity_type="admin",
        entity_id=admin.id,
        entity_label=admin.email,
        before=before,
        after={"is_active": active},
        ip_address=ip_address,
    )
    await db.commit()
    await db.refresh(admin)
    return admin


async def reset_admin_2fa(
    db: AsyncSession,
    actor: PlatformAdmin,
    admin_id: UUID,
    ip_address: str | None = None,
    generate_recovery_codes_flag: bool = False,
) -> PlatformAdmin:
    admin = await _get_admin(db, admin_id)
    plain_secret = generate_totp_secret()
    admin.totp_secret = encrypt_totp_secret(plain_secret)
    codes = generate_recovery_codes(10) if generate_recovery_codes_flag else None
    admin.recovery_codes_hashes = (
        [hash_recovery_code(admin.id, c) for c in codes] if codes else None
    )
    admin.last_totp_step = 0
    admin.token_version += 1  # old sessions end; they sign in again with the new secret
    admin.failed_login_attempts = 0
    admin.locked_until = None
    admin._plain_totp_secret = plain_secret
    admin._plain_recovery_codes = codes
    await record_audit(
        db,
        actor,
        action="admin.reset_2fa",
        entity_type="admin",
        entity_id=admin.id,
        entity_label=admin.email,
        after={"two_factor": "reset"},  # never log the secret
        ip_address=ip_address,
    )
    await db.commit()
    await db.refresh(admin)
    admin._plain_totp_secret = plain_secret
    admin._plain_recovery_codes = codes
    return admin


async def change_admin_password(
    db: AsyncSession,
    admin: PlatformAdmin,
    *,
    current_password: str,
    new_password: str,
    ip_address: str | None = None,
) -> tuple[str, int]:
    if not await asyncio.to_thread(verify_password, current_password, admin.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect"
        )
    if len(new_password) < 12:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be at least 12 characters",
        )
    if current_password == new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from current password",
        )
    admin.password_hash = await asyncio.to_thread(hash_password, new_password)
    admin.token_version += 1  # invalidates all existing sessions/tokens
    await record_audit(
        db,
        admin,
        action="auth.password_change",
        entity_type="admin",
        entity_id=admin.id,
        entity_label=admin.email,
        ip_address=ip_address,
    )
    await db.commit()
    await db.refresh(admin)
    token, expires_in = create_platform_token(admin.id, admin.token_version)
    return token, expires_in


async def revoke_platform_token(
    db: AsyncSession,
    admin: PlatformAdmin,
    token_payload: dict[str, Any],
    ip_address: str | None = None,
) -> None:
    jti = token_payload.get("jti")
    exp = token_payload.get("exp")
    if jti and exp:
        exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
        revoked = PlatformRevokedToken(
            jti=jti,
            admin_id=admin.id,
            expires_at=exp_dt,
        )
        db.add(revoked)
    await record_audit(
        db,
        admin,
        action="auth.logout",
        entity_type="admin",
        entity_id=admin.id,
        entity_label=admin.email,
        ip_address=ip_address,
    )
    await db.commit()


async def generate_admin_recovery_codes(
    db: AsyncSession,
    admin: PlatformAdmin,
    ip_address: str | None = None,
) -> list[str]:
    codes = generate_recovery_codes(10)
    admin.recovery_codes_hashes = [hash_recovery_code(admin.id, c) for c in codes]
    await record_audit(
        db,
        admin,
        action="auth.recovery_codes_generated",
        entity_type="admin",
        entity_id=admin.id,
        entity_label=admin.email,
        ip_address=ip_address,
    )
    await db.commit()
    await db.refresh(admin)
    return codes

