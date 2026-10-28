"""/platform/* - the admin web panel API. Admin JWT only (separate secret + audience)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import PlainTextResponse

from app.core.config import settings
from app.core.platform_security import create_platform_token, ensure_configured
from app.core.rate_limit import enforce_rate_limit
from app.database import get_db
from app.models.platform import PlatformAdmin
from app.platform_deps import get_platform_admin, request_ip
from app.schemas.platform import (
    AdminCreate,
    AdminCredentials,
    AdminOut,
    AuditPage,
    BusinessDetail,
    BusinessPage,
    DashboardOut,
    LicenseExtend,
    LicenseIssue,
    LicenseOut,
    LicensePage,
    LicenseTransfer,
    LicenseTransferResult,
    MessageOut,
    NoteCreate,
    NoteOut,
    PaymentCreate,
    PaymentResult,
    PlanIn,
    PlanOut,
    PlatformLoginRequest,
    PlatformSettingsBody,
    PlatformTokenResponse,
    ReasonBody,
    Reset2faResult,
    SubscriptionOut,
    SubscriptionPatch,
)
from app.services import license_service, platform_admin_service as admins, platform_service as svc

router = APIRouter(prefix="/platform", tags=["Platform admin"])
Admin = Depends(get_platform_admin)


def _admin_out(admin: PlatformAdmin) -> AdminOut:
    return AdminOut.model_validate(admin)


# ── auth ──────────────────────────────────────────────────────────────────────


@router.post("/auth/login", response_model=PlatformTokenResponse)
async def platform_login(data: PlatformLoginRequest, request: Request, db=Depends(get_db)):
    ensure_configured()  # 503 before any attempt is counted or any row is touched
    ip = request_ip(request)
    limits = settings.platform_rate_limits
    enforce_rate_limit("platform-login-ip", ip, limits["login_ip"])
    enforce_rate_limit("platform-login-account", data.email.strip().lower(), limits["login_account"])
    admin, token, expires_in = await admins.login(db, data.email, data.password, data.totp_code, ip)
    return PlatformTokenResponse(access_token=token, expires_in=expires_in, admin=_admin_out(admin))


@router.post("/auth/refresh", response_model=PlatformTokenResponse)
async def platform_refresh(admin: PlatformAdmin = Admin):
    """Swap a still-valid token for a fresh one (the panel calls this while an admin is active)."""
    token, expires_in = create_platform_token(admin.id, admin.token_version)
    return PlatformTokenResponse(access_token=token, expires_in=expires_in, admin=_admin_out(admin))


@router.post("/auth/logout", response_model=MessageOut)
async def platform_logout(
    request: Request, admin: PlatformAdmin = Admin, db=Depends(get_db)
):
    """Tokens are stateless; this records the sign-out. Disable/2FA-reset end all sessions."""
    await admins.record_audit(
        db, admin, action="auth.logout", entity_type="admin", entity_id=admin.id,
        entity_label=admin.email, ip_address=request_ip(request),
    )
    await db.commit()
    return MessageOut(message="Signed out")


@router.get("/me", response_model=AdminOut)
async def platform_me(admin: PlatformAdmin = Admin):
    return _admin_out(admin)


# ── dashboard ─────────────────────────────────────────────────────────────────


@router.get("/dashboard", response_model=DashboardOut)
async def dashboard(_: PlatformAdmin = Admin, db=Depends(get_db)):
    return await svc.dashboard(db)


# ── businesses ────────────────────────────────────────────────────────────────


@router.get("/businesses", response_model=BusinessPage)
async def list_businesses(
    search: str | None = Query(default=None, max_length=100),
    status_filter: Literal["trial", "active", "grace", "expired", "suspended", "cancelled"] | None = Query(
        default=None, alias="status"
    ),
    plan_id: UUID | None = Query(default=None),
    sort: str = Query(default="name"),
    order: Literal["asc", "desc"] = Query(default="asc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _: PlatformAdmin = Admin,
    db=Depends(get_db),
):
    return await svc.list_businesses(
        db, search=search, status_filter=status_filter, plan_id=plan_id,
        sort=sort, order=order, page=page, page_size=page_size,
    )


@router.get("/businesses/{business_id}", response_model=BusinessDetail)
async def business_detail(business_id: UUID, _: PlatformAdmin = Admin, db=Depends(get_db)):
    return await svc.business_detail(db, business_id)


@router.patch("/businesses/{business_id}/subscription", response_model=SubscriptionOut)
async def patch_subscription(
    business_id: UUID, data: SubscriptionPatch, request: Request,
    admin: PlatformAdmin = Admin, db=Depends(get_db),
):
    return await svc.patch_subscription(db, admin, business_id, data, request_ip(request))


@router.post(
    "/businesses/{business_id}/payments", response_model=PaymentResult,
    status_code=status.HTTP_201_CREATED,
)
async def record_payment(
    business_id: UUID, data: PaymentCreate, request: Request,
    admin: PlatformAdmin = Admin, db=Depends(get_db),
):
    return await svc.record_payment(db, admin, business_id, data, request_ip(request))


@router.post("/businesses/{business_id}/suspend", response_model=SubscriptionOut)
async def suspend_business(
    business_id: UUID, request: Request, data: ReasonBody | None = None,
    admin: PlatformAdmin = Admin, db=Depends(get_db),
):
    return await svc.set_suspended(
        db, admin, business_id, True, data.reason if data else None, request_ip(request)
    )


@router.post("/businesses/{business_id}/activate", response_model=SubscriptionOut)
async def activate_business(
    business_id: UUID, request: Request, data: ReasonBody | None = None,
    admin: PlatformAdmin = Admin, db=Depends(get_db),
):
    return await svc.set_suspended(
        db, admin, business_id, False, data.reason if data else None, request_ip(request)
    )


@router.post(
    "/businesses/{business_id}/notes", response_model=NoteOut, status_code=status.HTTP_201_CREATED
)
async def add_business_note(
    business_id: UUID, data: NoteCreate, request: Request,
    admin: PlatformAdmin = Admin, db=Depends(get_db),
):
    return await svc.add_note(db, admin, business_id, data.text, request_ip(request))


# ── plans ─────────────────────────────────────────────────────────────────────


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(_: PlatformAdmin = Admin, db=Depends(get_db)):
    return await svc.list_plans(db)


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
async def create_plan(data: PlanIn, request: Request, admin: PlatformAdmin = Admin, db=Depends(get_db)):
    return await svc.create_plan(db, admin, data, request_ip(request))


@router.put("/plans/{plan_id}", response_model=PlanOut)
async def update_plan(
    plan_id: UUID, data: PlanIn, request: Request, admin: PlatformAdmin = Admin, db=Depends(get_db)
):
    return await svc.update_plan(db, admin, plan_id, data, request_ip(request))


@router.post("/plans/{plan_id}/disable", response_model=PlanOut)
async def disable_plan(plan_id: UUID, request: Request, admin: PlatformAdmin = Admin, db=Depends(get_db)):
    return await svc.set_plan_active(db, admin, plan_id, False, request_ip(request))


@router.post("/plans/{plan_id}/enable", response_model=PlanOut)
async def enable_plan(plan_id: UUID, request: Request, admin: PlatformAdmin = Admin, db=Depends(get_db)):
    return await svc.set_plan_active(db, admin, plan_id, True, request_ip(request))


@router.delete("/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_plan(plan_id: UUID, request: Request, admin: PlatformAdmin = Admin, db=Depends(get_db)):
    await svc.delete_plan(db, admin, plan_id, request_ip(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── settings ──────────────────────────────────────────────────────────────────


@router.get("/settings", response_model=PlatformSettingsBody)
async def get_settings(_: PlatformAdmin = Admin, db=Depends(get_db)):
    return await svc.get_settings(db)


@router.put("/settings", response_model=PlatformSettingsBody)
async def put_settings(
    data: PlatformSettingsBody, request: Request, admin: PlatformAdmin = Admin, db=Depends(get_db)
):
    return await svc.update_settings(db, admin, data, request_ip(request))


# ── platform admins ───────────────────────────────────────────────────────────


@router.get("/admins", response_model=list[AdminOut])
async def list_admins(_: PlatformAdmin = Admin, db=Depends(get_db)):
    return [_admin_out(a) for a in await admins.list_admins(db)]


@router.post("/admins", response_model=AdminCredentials, status_code=status.HTTP_201_CREATED)
async def create_admin(
    data: AdminCreate, request: Request, admin: PlatformAdmin = Admin, db=Depends(get_db)
):
    created = await admins.create_admin(
        db, admin, email=data.email, name=data.name, password=data.password,
        ip_address=request_ip(request),
    )
    return AdminCredentials(admin=_admin_out(created), **admins.credentials_for(created))


@router.post("/admins/{admin_id}/disable", response_model=AdminOut)
async def disable_admin(
    admin_id: UUID, request: Request, admin: PlatformAdmin = Admin, db=Depends(get_db)
):
    return _admin_out(await admins.set_admin_active(db, admin, admin_id, False, request_ip(request)))


@router.post("/admins/{admin_id}/enable", response_model=AdminOut)
async def enable_admin(
    admin_id: UUID, request: Request, admin: PlatformAdmin = Admin, db=Depends(get_db)
):
    return _admin_out(await admins.set_admin_active(db, admin, admin_id, True, request_ip(request)))


@router.post("/admins/{admin_id}/reset-2fa", response_model=Reset2faResult)
async def reset_admin_2fa(
    admin_id: UUID, request: Request, admin: PlatformAdmin = Admin, db=Depends(get_db)
):
    target = await admins.reset_admin_2fa(db, admin, admin_id, request_ip(request))
    return Reset2faResult(**admins.credentials_for(target))


# ── audit log ─────────────────────────────────────────────────────────────────


@router.get("/audit", response_model=AuditPage)
async def audit_log(
    admin: str | None = Query(default=None, max_length=255),
    action: str | None = Query(default=None, max_length=80),
    entity_type: str | None = Query(default=None, max_length=50),
    business_id: UUID | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    _: PlatformAdmin = Admin,
    db=Depends(get_db),
):
    return await svc.list_audit(
        db, admin=admin, action=action, entity_type=entity_type, business_id=business_id,
        date_from=date_from, date_to=date_to, page=page, page_size=page_size,
    )


# ── licenses ──────────────────────────────────────────────────────────────────


@router.post("/licenses", response_model=LicenseOut, status_code=status.HTTP_201_CREATED)
async def issue_license(
    data: LicenseIssue, request: Request, admin: PlatformAdmin = Admin, db=Depends(get_db)
):
    return await license_service.issue_license(db, admin, data, request_ip(request))


@router.get("/licenses", response_model=LicensePage)
async def list_licenses(
    search: str | None = Query(default=None, max_length=100),
    status_filter: Literal["issued", "active", "revoked"] | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _: PlatformAdmin = Admin,
    db=Depends(get_db),
):
    return await license_service.list_licenses(
        db, search=search, status_filter=status_filter, page=page, page_size=page_size
    )


@router.get("/licenses/{license_id}", response_model=LicenseOut)
async def get_license(license_id: UUID, _: PlatformAdmin = Admin, db=Depends(get_db)):
    return await license_service.get_license(db, license_id)


@router.get("/licenses/{license_id}/file", response_class=PlainTextResponse)
async def license_file(license_id: UUID, _: PlatformAdmin = Admin, db=Depends(get_db)):
    lic = await license_service.get_license(db, license_id)
    return PlainTextResponse(
        license_service.license_file_text(lic),
        headers={"Content-Disposition": 'attachment; filename="license.txt"'},
    )


@router.post("/licenses/{license_id}/revoke", response_model=LicenseOut)
async def revoke_license(
    license_id: UUID, request: Request, admin: PlatformAdmin = Admin, db=Depends(get_db)
):
    return await license_service.revoke_license(db, admin, license_id, request_ip(request))


@router.post("/licenses/{license_id}/transfer", response_model=LicenseTransferResult)
async def transfer_license(
    license_id: UUID, data: LicenseTransfer, request: Request,
    admin: PlatformAdmin = Admin, db=Depends(get_db),
):
    old, new = await license_service.transfer_license(
        db, admin, license_id, data.machine_id, data.notes, request_ip(request)
    )
    return LicenseTransferResult(old=old, new=new)


@router.post("/licenses/{license_id}/extend-updates", response_model=LicenseOut)
async def extend_license_updates(
    license_id: UUID, data: LicenseExtend, request: Request,
    admin: PlatformAdmin = Admin, db=Depends(get_db),
):
    return await license_service.extend_updates(
        db, admin, license_id, data.updates_until, request_ip(request)
    )

