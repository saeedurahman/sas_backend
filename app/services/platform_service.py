"""Platform-owner operations on businesses, subscriptions, plans, settings and audit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import AuditLog  # noqa: F401  (tenant audit stays tenant-side)
from app.models.business import Branch, Business
from app.models.platform import (
    Plan,
    PlatformAdmin,
    PlatformAuditLog,
    PlatformBusinessNote,
    PlatformSetting,
    SubscriptionPayment,
)

from app.models.user import RefreshToken, Role, User, UserRole
from app.schemas.platform import PaymentCreate, PlanIn, PlatformSettingsBody, SubscriptionPatch
from app.services import subscription as sub
from app.services.platform_admin_service import record_audit

PLATFORM_TZ = ZoneInfo("Asia/Karachi")
PAGE_SIZE_MAX = 100
SETTINGS_KEYS = ("contact_phone", "contact_whatsapp", "contact_email", "payment_instructions")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── effective status, in SQL and in Python (they must agree) ──────────────────


def status_case(now: datetime, grace_days: int | None = None):
    grace = timedelta(days=settings.grace_days if grace_days is None else grace_days)
    b = Business
    return case(
        (b.is_active.is_(False), "suspended"),
        (b.subscription_status == "suspended", "suspended"),
        (b.subscription_status == "cancelled", "cancelled"),
        (
            and_(b.subscription_status == "trial", b.trial_ends_at.is_not(None), b.trial_ends_at < now),
            "expired",
        ),
        (b.subscription_status == "trial", "trial"),
        (or_(b.subscription_ends_at.is_(None), b.subscription_ends_at >= now), "active"),
        (b.subscription_ends_at + grace >= now, "grace"),
        else_="expired",
    )


def expires_case():
    b = Business
    return case((b.subscription_status == "trial", b.trial_ends_at), else_=b.subscription_ends_at)


def effective_status(business: Business, now: datetime | None = None) -> sub.SubscriptionState:
    state = sub.compute_state(business, now)
    if not business.is_active:
        return sub.SubscriptionState(
            sub.SUSPENDED, state.trial_ends_at, state.subscription_ends_at, None, None, False
        )
    return state


# ── dashboard ─────────────────────────────────────────────────────────────────


def month_start_utc(now: datetime | None = None) -> datetime:
    local = (now or _now()).astimezone(PLATFORM_TZ)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


async def dashboard(db: AsyncSession) -> dict[str, Any]:
    now = _now()
    st = status_case(now)
    ex = expires_case()
    alive = Business.deleted_at.is_(None)

    counts = dict(
        (await db.execute(select(st, func.count()).where(alive).group_by(st))).all()
    )
    expiring = (
        await db.execute(
            select(func.count()).where(
                alive,
                st.in_(("active", "trial")),
                ex.is_not(None),
                ex >= now,
                ex <= now + timedelta(days=7),
            )
        )
    ).scalar_one()
    month_total = (
        await db.execute(
            select(func.coalesce(func.sum(SubscriptionPayment.amount_pkr), 0)).where(
                SubscriptionPayment.created_at >= month_start_utc(now)
            )
        )
    ).scalar_one()

    attention_rows = (
        await db.execute(
            select(Business, st.label("eff"))
            .where(
                alive,
                or_(
                    st == "grace",
                    and_(
                        st.in_(("active", "trial")),
                        ex.is_not(None),
                        ex <= now + timedelta(days=7),
                    ),
                ),
            )
            .order_by(ex.asc().nulls_last())
            .limit(50)
        )
    ).all()
    attention = []
    for business, eff in attention_rows:
        state = sub.compute_state(business, now)
        attention.append(
            {
                "business_id": business.id,
                "name": business.name,
                "owner_phone": business.phone,
                "status": eff,
                "expires_at": state.expires_at,
                "days_left": state.days_left,
            }
        )
    return {
        "active": counts.get("active", 0),
        "trial": counts.get("trial", 0),
        "expiring_7d": expiring,
        "grace": counts.get("grace", 0),
        "suspended": counts.get("suspended", 0),
        "expired": counts.get("expired", 0),
        "cancelled": counts.get("cancelled", 0),
        "payments_this_month_pkr": Decimal(month_total),
        "needs_attention": attention,
    }


# ── businesses ────────────────────────────────────────────────────────────────

_SORTS = {"name", "created_at", "expires_at", "status", "last_active_at", "branches", "users"}


def _count_subquery(model, fk_column, *extra):
    return (
        select(func.count())
        .where(fk_column == Business.id, model.deleted_at.is_(None), *extra)
        .correlate(Business)
        .scalar_subquery()
    )


def _last_active_subquery():
    last_login = (
        select(func.max(User.last_login_at)).where(User.business_id == Business.id).correlate(Business).scalar_subquery()
    )
    last_refresh = (
        select(func.max(RefreshToken.created_at))
        .where(RefreshToken.business_id == Business.id)
        .correlate(Business)
        .scalar_subquery()
    )
    return func.greatest(last_login, last_refresh)


async def list_businesses(
    db: AsyncSession,
    *,
    search: str | None,
    status_filter: str | None,
    plan_id: UUID | None,
    sort: str,
    order: str,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    if sort not in _SORTS:
        raise HTTPException(status_code=400, detail=f"sort must be one of {sorted(_SORTS)}")
    page_size = max(1, min(page_size, PAGE_SIZE_MAX))
    page = max(1, page)
    now = _now()
    st = status_case(now)
    ex = expires_case()
    branches = _count_subquery(Branch, Branch.business_id, Branch.is_active.is_(True))
    users = _count_subquery(User, User.business_id)
    last_active = _last_active_subquery()

    filters = [Business.deleted_at.is_(None)]
    if search and search.strip():
        like = f"%{search.strip().replace('%', r'\%').replace('_', r'\_')}%"
        filters.append(or_(Business.name.ilike(like), Business.phone.ilike(like), Business.email.ilike(like)))
    if status_filter:
        filters.append(st == status_filter)
    if plan_id:
        filters.append(Business.plan_id == plan_id)

    sort_expr = {
        "name": func.lower(Business.name),
        "created_at": Business.created_at,
        "expires_at": ex,
        "status": st,
        "last_active_at": last_active,
        "branches": branches,
        "users": users,
    }[sort]
    ordering = sort_expr.desc().nulls_last() if order == "desc" else sort_expr.asc().nulls_last()

    total = (await db.execute(select(func.count()).select_from(Business).where(*filters))).scalar_one()
    rows = (
        await db.execute(
            select(
                Business,
                st.label("eff"),
                Plan.name.label("plan_name"),
                branches.label("branch_count"),
                users.label("user_count"),
                last_active.label("last_active"),
            )
            .outerjoin(Plan, Plan.id == Business.plan_id)
            .where(*filters)
            .order_by(ordering, Business.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    items = []
    for business, eff, plan_name, branch_count, user_count, last_active_at in rows:
        state = sub.compute_state(business, now)
        items.append(
            {
                "id": business.id,
                "name": business.name,
                "owner_phone": business.phone,
                "plan_id": business.plan_id,
                "plan_name": plan_name,
                "status": eff,
                "expires_at": state.expires_at,
                "days_left": state.days_left,
                "branches": branch_count,
                "users": user_count,
                "last_active_at": last_active_at,
                "created_at": business.created_at,
            }
        )
    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def _load_business(db: AsyncSession, business_id: UUID, *, lock: bool = False) -> Business:
    stmt = select(Business).where(Business.id == business_id, Business.deleted_at.is_(None))
    if lock:
        stmt = stmt.with_for_update(of=Business)
    business = (await db.execute(stmt)).scalar_one_or_none()
    if business is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Business not found")
    return business


async def _plan_name(db: AsyncSession, plan_id: UUID | None) -> str | None:
    if plan_id is None:
        return None
    return (await db.execute(select(Plan.name).where(Plan.id == plan_id))).scalar_one_or_none()


async def subscription_view(db: AsyncSession, business: Business) -> dict[str, Any]:
    state = effective_status(business)
    return {
        "plan_id": business.plan_id,
        "plan_name": await _plan_name(db, business.plan_id),
        "status": state.status,
        "stored_status": business.subscription_status,
        "trial_ends_at": business.trial_ends_at,
        "subscription_ends_at": business.subscription_ends_at,
        "expires_at": state.expires_at,
        "days_left": state.days_left,
        "in_grace": state.in_grace,
    }


def _snapshot(business: Business) -> dict[str, Any]:
    return {
        "plan_id": business.plan_id,
        "status": business.subscription_status,
        "trial_ends_at": business.trial_ends_at,
        "subscription_ends_at": business.subscription_ends_at,
    }


async def _payment_out(db: AsyncSession, payments: list[SubscriptionPayment]) -> list[dict[str, Any]]:
    ids = {p.recorded_by for p in payments if p.recorded_by}
    emails: dict[UUID, str] = {}
    if ids:
        emails = dict(
            (await db.execute(select(PlatformAdmin.id, PlatformAdmin.email).where(PlatformAdmin.id.in_(ids)))).all()
        )
    out = []
    for p in payments:
        out.append(
            {
                "id": p.id,
                "business_id": p.business_id,
                "plan_id": p.plan_id,
                "amount_pkr": p.amount_pkr,
                "method": p.method,
                "reference": p.reference,
                "note": p.note,
                "period_months": p.period_months,
                "period_start": p.period_start,
                "period_end": p.period_end,
                "recorded_by": p.recorded_by,
                "recorded_by_email": emails.get(p.recorded_by) if p.recorded_by else None,
                "created_at": p.created_at,
            }
        )
    return out


async def business_detail(db: AsyncSession, business_id: UUID) -> dict[str, Any]:
    business = await _load_business(db, business_id)
    owner = (
        await db.execute(
            select(User)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                User.business_id == business_id,
                User.deleted_at.is_(None),
                UserRole.deleted_at.is_(None),
                func.lower(Role.name) == "owner",
            )
            .order_by(User.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    branches = (
        await db.execute(
            select(func.count()).where(
                Branch.business_id == business_id, Branch.deleted_at.is_(None), Branch.is_active.is_(True)
            )
        )
    ).scalar_one()
    users = (
        await db.execute(select(func.count()).where(User.business_id == business_id, User.deleted_at.is_(None)))
    ).scalar_one()
    last_active = (
        await db.execute(select(_last_active_subquery()).where(Business.id == business_id))
    ).scalar_one()
    payments = list(
        (
            await db.execute(
                select(SubscriptionPayment)
                .where(SubscriptionPayment.business_id == business_id)
                .order_by(SubscriptionPayment.created_at.desc())
                .limit(100)
            )
        ).scalars()
    )
    audit = list(
        (
            await db.execute(
                select(PlatformAuditLog)
                .where(PlatformAuditLog.business_id == business_id)
                .order_by(PlatformAuditLog.created_at.desc())
                .limit(20)
            )
        ).scalars()
    )
    notes = list(
        (
            await db.execute(
                select(PlatformBusinessNote)
                .where(PlatformBusinessNote.business_id == business_id)
                .order_by(PlatformBusinessNote.created_at.desc())
                .limit(100)
            )
        ).scalars()
    )
    address = ", ".join(p for p in (business.address_line1, business.city) if p) or None
    return {
        "id": business.id,
        "name": business.name,
        "owner_name": owner.full_name if owner else None,
        "owner_phone": owner.phone if owner else business.phone,
        "owner_email": business.email,
        "address": address,
        "city": business.city,
        "is_active": business.is_active,
        "created_at": business.created_at,
        "branches": branches,
        "users": users,
        "last_active_at": last_active,
        "subscription": await subscription_view(db, business),
        "payments": await _payment_out(db, payments),
        "recent_audit": audit,
        "notes": notes,
    }


async def patch_subscription(
    db: AsyncSession,
    admin: PlatformAdmin,
    business_id: UUID,
    data: SubscriptionPatch,
    ip: str | None,
) -> dict[str, Any]:
    business = await _load_business(db, business_id, lock=True)
    before = _snapshot(business)
    now = _now()

    if data.plan_id is not None:
        plan = await db.get(Plan, data.plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="Plan not found")
        if not plan.is_active and plan.id != business.plan_id:
            raise HTTPException(status_code=400, detail="This plan is disabled")
        business.plan_id = plan.id

    if data.status is not None:
        business.subscription_status = data.status

    if data.subscription_ends_at is not None:
        end = data.subscription_ends_at
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        business.subscription_ends_at = end
        if business.subscription_status == "trial":
            business.subscription_status = "active"  # a dated paid period replaces the trial

    if data.extend_days is not None:
        delta = timedelta(days=data.extend_days)
        if business.subscription_status == "trial":
            base = max(now, business.trial_ends_at or now)
            business.trial_ends_at = base + delta
        else:
            base = max(now, business.subscription_ends_at or now)
            business.subscription_ends_at = base + delta

    await record_audit(
        db,
        admin,
        action="business.subscription_update",
        entity_type="business",
        entity_id=business.id,
        entity_label=business.name,
        business_id=business.id,
        before=before,
        after={**_snapshot(business), **({"note": data.note} if data.note else {})},
        ip_address=ip,
    )
    await db.commit()
    await db.refresh(business)
    return await subscription_view(db, business)


async def record_payment(
    db: AsyncSession,
    admin: PlatformAdmin,
    business_id: UUID,
    data: PaymentCreate,
    ip: str | None,
) -> dict[str, Any]:
    business = await _load_business(db, business_id, lock=True)
    before = _snapshot(business)
    now = _now()

    plan_id = data.plan_id or business.plan_id
    if data.plan_id is not None and await db.get(Plan, data.plan_id) is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    # New period starts when the current one ends (paying early never loses days).
    candidates = [now]
    if business.subscription_ends_at is not None:
        candidates.append(business.subscription_ends_at)
    if business.subscription_status == "trial" and business.trial_ends_at is not None:
        candidates.append(business.trial_ends_at)
    start = max(candidates)
    end = sub.add_months(start, data.period_months)

    payment = SubscriptionPayment(
        business_id=business.id,
        plan_id=plan_id,
        amount_pkr=data.amount_pkr,
        method=data.method,
        reference=(data.reference or "").strip() or None,
        note=data.note,
        period_months=data.period_months,
        period_start=start,
        period_end=end,
        recorded_by=admin.id,
    )
    db.add(payment)
    business.subscription_ends_at = end
    business.subscription_status = "active"  # also reactivates suspended / expired / cancelled
    business.plan_id = plan_id
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A payment with this reference was already recorded for this business",
        ) from None

    await record_audit(
        db,
        admin,
        action="payment.record",
        entity_type="payment",
        entity_id=payment.id,
        entity_label=business.name,
        business_id=business.id,
        before=before,
        after={
            **_snapshot(business),
            "amount_pkr": data.amount_pkr,
            "method": data.method,
            "reference": payment.reference,
            "period_months": data.period_months,
        },
        ip_address=ip,
    )
    await db.commit()
    await db.refresh(business)
    await db.refresh(payment)
    return {
        "payment": (await _payment_out(db, [payment]))[0],
        "subscription": await subscription_view(db, business),
    }


async def set_suspended(
    db: AsyncSession,
    admin: PlatformAdmin,
    business_id: UUID,
    suspend: bool,
    reason: str | None,
    ip: str | None,
) -> dict[str, Any]:
    business = await _load_business(db, business_id, lock=True)
    before = _snapshot(business)
    if suspend:
        business.subscription_status = "suspended"
    else:
        if business.subscription_status not in ("suspended", "cancelled"):
            raise HTTPException(status_code=400, detail="This business is not suspended")
        # Back to what its dates say: a paid period if it ever had one, else the trial.
        business.subscription_status = (
            "active" if business.subscription_ends_at is not None else "trial"
        )
    await record_audit(
        db,
        admin,
        action="business.suspend" if suspend else "business.activate",
        entity_type="business",
        entity_id=business.id,
        entity_label=business.name,
        business_id=business.id,
        before=before,
        after={**_snapshot(business), **({"reason": reason} if reason else {})},
        ip_address=ip,
    )
    await db.commit()
    await db.refresh(business)
    return await subscription_view(db, business)


async def add_note(
    db: AsyncSession, admin: PlatformAdmin, business_id: UUID, text: str, ip: str | None
) -> PlatformBusinessNote:
    business = await _load_business(db, business_id)
    note = PlatformBusinessNote(
        business_id=business.id, admin_id=admin.id, admin_email=admin.email, text=text.strip()
    )
    db.add(note)
    await db.flush()
    await record_audit(
        db,
        admin,
        action="business.note",
        entity_type="business",
        entity_id=business.id,
        entity_label=business.name,
        business_id=business.id,
        after={"note": note.text[:200]},
        ip_address=ip,
    )
    await db.commit()
    await db.refresh(note)
    return note


# ── plans ─────────────────────────────────────────────────────────────────────


async def _plan_out(db: AsyncSession, plan: Plan) -> dict[str, Any]:
    count = (
        await db.execute(select(func.count()).where(Business.plan_id == plan.id, Business.deleted_at.is_(None)))
    ).scalar_one()
    return {
        "id": plan.id,
        "name": plan.name,
        "monthly_price_pkr": plan.monthly_price_pkr,
        "onetime_price_pkr": plan.onetime_price_pkr,
        "notes": plan.notes,
        "is_active": plan.is_active,
        "business_count": count,
    }


async def list_plans(db: AsyncSession) -> list[dict[str, Any]]:
    plans = (await db.execute(select(Plan).order_by(Plan.monthly_price_pkr, Plan.name))).scalars().all()
    return [await _plan_out(db, p) for p in plans]


def _plan_snapshot(plan: Plan) -> dict[str, Any]:
    return {
        "name": plan.name,
        "monthly_price_pkr": plan.monthly_price_pkr,
        "onetime_price_pkr": plan.onetime_price_pkr,
        "notes": plan.notes,
        "is_active": plan.is_active,
    }


async def create_plan(db: AsyncSession, admin: PlatformAdmin, data: PlanIn, ip: str | None) -> dict[str, Any]:
    plan = Plan(**data.model_dump())
    plan.name = plan.name.strip()
    db.add(plan)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A plan with this name already exists") from None
    await record_audit(
        db, admin, action="plan.create", entity_type="plan", entity_id=plan.id,
        entity_label=plan.name, after=_plan_snapshot(plan), ip_address=ip,
    )
    await db.commit()
    await db.refresh(plan)
    return await _plan_out(db, plan)


async def _get_plan(db: AsyncSession, plan_id: UUID) -> Plan:
    plan = await db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan


async def update_plan(
    db: AsyncSession, admin: PlatformAdmin, plan_id: UUID, data: PlanIn, ip: str | None
) -> dict[str, Any]:
    plan = await _get_plan(db, plan_id)
    before = _plan_snapshot(plan)
    plan.name = data.name.strip()
    plan.monthly_price_pkr = data.monthly_price_pkr
    plan.onetime_price_pkr = data.onetime_price_pkr
    plan.notes = data.notes
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A plan with this name already exists") from None
    await record_audit(
        db, admin, action="plan.update", entity_type="plan", entity_id=plan.id,
        entity_label=plan.name, before=before, after=_plan_snapshot(plan), ip_address=ip,
    )
    await db.commit()
    await db.refresh(plan)
    return await _plan_out(db, plan)


async def set_plan_active(
    db: AsyncSession, admin: PlatformAdmin, plan_id: UUID, active: bool, ip: str | None
) -> dict[str, Any]:
    plan = await _get_plan(db, plan_id)
    before = _plan_snapshot(plan)
    plan.is_active = active
    await record_audit(
        db, admin, action="plan.enable" if active else "plan.disable", entity_type="plan",
        entity_id=plan.id, entity_label=plan.name, before=before, after=_plan_snapshot(plan),
        ip_address=ip,
    )
    await db.commit()
    await db.refresh(plan)
    return await _plan_out(db, plan)


async def delete_plan(db: AsyncSession, admin: PlatformAdmin, plan_id: UUID, ip: str | None) -> None:
    plan = await _get_plan(db, plan_id)
    in_use = (
        await db.execute(select(func.count()).where(Business.plan_id == plan.id))
    ).scalar_one()
    if in_use:
        raise HTTPException(
            status_code=409,
            detail=f"{in_use} business(es) use this plan. Disable it instead of deleting.",
        )
    before = _plan_snapshot(plan)
    await db.delete(plan)
    await record_audit(
        db, admin, action="plan.delete", entity_type="plan", entity_id=plan_id,
        entity_label=plan.name, before=before, ip_address=ip,
    )
    await db.commit()


# ── settings ──────────────────────────────────────────────────────────────────


async def get_settings(db: AsyncSession) -> dict[str, str]:
    values = await sub.load_platform_settings(db)
    return {key: values.get(key, "") or "" for key in SETTINGS_KEYS}


async def update_settings(
    db: AsyncSession, admin: PlatformAdmin, data: PlatformSettingsBody, ip: str | None
) -> dict[str, str]:
    before = await get_settings(db)
    after = {key: (getattr(data, key) or "").strip() for key in SETTINGS_KEYS}
    existing = {s.key: s for s in (await db.execute(select(PlatformSetting))).scalars()}
    for key, value in after.items():
        row = existing.get(key)
        if row is None:
            db.add(PlatformSetting(key=key, value=value, updated_by=admin.id))
        else:
            row.value = value
            row.updated_by = admin.id
    await record_audit(
        db, admin, action="settings.update", entity_type="settings", entity_id="platform",
        entity_label="Support & payment settings", before=before, after=after, ip_address=ip,
    )
    await db.commit()
    return after


# ── audit log ─────────────────────────────────────────────────────────────────


async def list_audit(
    db: AsyncSession,
    *,
    admin: str | None,
    action: str | None,
    entity_type: str | None,
    business_id: UUID | None,
    date_from: datetime | None,
    date_to: datetime | None,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    page_size = max(1, min(page_size, PAGE_SIZE_MAX))
    page = max(1, page)
    filters = []
    if admin:
        filters.append(PlatformAuditLog.admin_email.ilike(f"%{admin.strip()}%"))
    if action:
        filters.append(PlatformAuditLog.action.ilike(f"%{action.strip()}%"))
    if entity_type:
        filters.append(PlatformAuditLog.entity_type == entity_type)
    if business_id:
        filters.append(PlatformAuditLog.business_id == business_id)
    if date_from:
        filters.append(PlatformAuditLog.created_at >= date_from)
    if date_to:
        filters.append(PlatformAuditLog.created_at < date_to + timedelta(days=1))
    total = (await db.execute(select(func.count()).select_from(PlatformAuditLog).where(*filters))).scalar_one()
    rows = (
        await db.execute(
            select(PlatformAuditLog)
            .where(*filters)
            .order_by(PlatformAuditLog.created_at.desc(), PlatformAuditLog.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return {"items": list(rows), "total": total, "page": page, "page_size": page_size}
