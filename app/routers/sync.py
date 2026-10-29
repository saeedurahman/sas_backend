"""Offline sync API: change feed (pull/snapshot), op push, flagged-sale review."""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import text

from app.branch_scope import scoped_branch_query
from app.database import get_db
from app.dependencies import require_any_permission, require_manager, subscription_exempt
from app.models.user import User
from app.schemas.sync import (
    FlaggedSalesResponse,
    PullResponse,
    PushRequest,
    PushResponse,
    SnapshotResponse,
)
from app.core.config import settings
from app.services import sync_feed
from app.services.sync_devices import rate_limit_device, touch_device
from app.services.sync_push import PUSH_PERMISSIONS, process_push

router = APIRouter(prefix="/sync", tags=["Sync"])

# Sync must keep working for lapsed subscriptions (offline sales are already paid for);
# the handlers apply the subscription rules themselves.
_pull_guard = require_any_permission(*sync_feed.PULL_PERMISSIONS)
_push_guard = require_any_permission(*PUSH_PERMISSIONS)


def _device(
    device_id: str | None = Query(default=None, max_length=100),
    x_device_id: str | None = Header(default=None, max_length=100),
) -> str:
    value = device_id or x_device_id
    if not value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "device_id_required", "message": "device_id (query or X-Device-ID) is required"},
        )
    return value


@router.get(
    "/pull",
    response_model=PullResponse,
    dependencies=[Depends(subscription_exempt)],
)
async def pull_changes(
    since: str | None = Query(default=None, description="Opaque cursor from the previous response"),
    limit: int = Query(default=sync_feed.DEFAULT_LIMIT, ge=1, le=sync_feed.MAX_LIMIT),
    branch_id: UUID | None = Query(default=None, description="Narrow branch-scoped entities to one branch"),
    device_id: str = Depends(_device),
    app_version: str | None = Header(default=None, alias="X-App-Version", max_length=50),
    current_user: User = Depends(_pull_guard),
    db=Depends(get_db),
):
    business_id, user_id = current_user.business_id, current_user.id
    rate_limit_device("pull", business_id, device_id)
    await touch_device(
        db, business_id=business_id, user_id=user_id, device_id=device_id, app_version=app_version
    )
    return await sync_feed.pull(
        db, current_user, since=since, limit=limit, branch_id=branch_id
    )


@router.get(
    "/snapshot",
    response_model=SnapshotResponse,
    dependencies=[Depends(subscription_exempt)],
)
async def snapshot(
    page_token: str | None = Query(default=None, description="From the previous page; omit for the first"),
    limit: int = Query(default=sync_feed.DEFAULT_LIMIT, ge=1, le=sync_feed.MAX_LIMIT),
    branch_id: UUID | None = Query(default=None, description="Narrow branch-scoped entities to one branch"),
    device_id: str = Depends(_device),
    app_version: str | None = Header(default=None, alias="X-App-Version", max_length=50),
    current_user: User = Depends(_pull_guard),
    db=Depends(get_db),
):
    business_id, user_id = current_user.business_id, current_user.id
    rate_limit_device("pull", business_id, device_id)
    await touch_device(
        db, business_id=business_id, user_id=user_id, device_id=device_id, app_version=app_version
    )
    return await sync_feed.snapshot(
        db, current_user, page_token=page_token, limit=limit, branch_id=branch_id
    )


@router.post(
    "/push",
    response_model=PushResponse,
    dependencies=[Depends(subscription_exempt)],
)
async def push_ops(
    data: PushRequest,
    current_user: User = Depends(_push_guard),
    db=Depends(get_db),
):
    if len(data.ops) > settings.sync_max_ops:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": "batch_too_large",
                "message": f"At most {settings.sync_max_ops} ops per push",
                "max_ops": settings.sync_max_ops,
            },
        )
    rate_limit_device("push", current_user.business_id, data.device_id)
    await touch_device(
        db,
        business_id=current_user.business_id,
        user_id=current_user.id,
        device_id=data.device_id,
        name=data.device_name,
        app_version=data.app_version,
    )
    results = await process_push(db, current_user, data)
    return PushResponse(
        batch_id=data.batch_id, server_time=datetime.now(timezone.utc), results=results
    )


@router.get("/flags", response_model=FlaggedSalesResponse)
async def flagged_sales(
    branch_id: UUID | None = Depends(scoped_branch_query),
    flag: str | None = Query(default=None, description="Only sales carrying this flag"),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    """Offline sales the server accepted but flagged for a human to look at (owner/manager)."""
    where = [
        "s.business_id = :business_id",
        "s.deleted_at IS NULL",
        "s.sync_flags <> '[]'::jsonb",
    ]
    params: dict = {"business_id": current_user.business_id, "skip": skip, "limit": limit}
    if branch_id is not None:
        where.append("s.branch_id = :branch_id")
        params["branch_id"] = branch_id
    if flag:
        where.append("s.sync_flags ? :flag")
        params["flag"] = flag
    if date_from is not None:
        where.append("s.synced_at >= :date_from")
        params["date_from"] = date_from
    if date_to is not None:
        where.append("s.synced_at <= :date_to")
        params["date_to"] = date_to
    clause = " AND ".join(where)

    total = (
        await db.execute(text(f"SELECT count(*) FROM sales s WHERE {clause}"), params)  # noqa: S608
    ).scalar_one()
    rows = (
        await db.execute(
            text(
                f"""
                SELECT s.id, s.branch_id, s.sale_number, s.client_receipt_no, s.device_id,
                       s.sold_at, s.synced_at, s.sync_flags, s.status,
                       COALESCE((SELECT SUM(l.qty * l.unit_price - l.discount_amount + l.tax_amount)
                                   FROM sale_lines l
                                  WHERE l.sale_id = s.id AND l.deleted_at IS NULL), 0) AS total_amount,
                       COALESCE(u.full_name, '') AS cashier_name
                  FROM sales s LEFT JOIN users u ON u.id = s.created_by
                 WHERE {clause}
                 ORDER BY s.synced_at DESC NULLS LAST, s.id
                 OFFSET :skip LIMIT :limit
                """  # noqa: S608 - clause is built from constants
            ),
            params,
        )
    ).mappings().all()
    items = [
        {
            "id": r["id"],
            "branch_id": r["branch_id"],
            "sale_number": r["sale_number"],
            "client_receipt_no": r["client_receipt_no"],
            "device_id": r["device_id"],
            "sold_at": r["sold_at"],
            "synced_at": r["synced_at"],
            "flags": list(r["sync_flags"] or []),
            "status": r["status"],
            "total_amount": r["total_amount"],
            "cashier_name": r["cashier_name"],
        }
        for r in rows
    ]
    return FlaggedSalesResponse(total=total, skip=skip, limit=limit, items=items)
