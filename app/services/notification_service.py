"""Notification services."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import NotificationChannelEnum, NotificationTypeEnum
from app.models.settings import NotificationLog

_ALERT_LOOKBACK = timedelta(hours=24)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_notifications(
    db: AsyncSession,
    business_id: UUID,
    user_id: UUID | None = None,
    branch_id: UUID | None = None,
    is_read: bool | None = None,
    notification_type: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[NotificationLog], int, int]:
    filters = [
        NotificationLog.business_id == business_id,
        NotificationLog.deleted_at.is_(None),
    ]
    if user_id is not None:
        filters.append(
            (NotificationLog.user_id.is_(None)) | (NotificationLog.user_id == user_id)
        )
    if branch_id is not None:
        filters.append(NotificationLog.branch_id == branch_id)
    if is_read is not None:
        filters.append(NotificationLog.is_read == is_read)
    if notification_type is not None:
        filters.append(NotificationLog.notification_type == notification_type)

    count_result = await db.execute(
        select(func.count()).select_from(NotificationLog).where(*filters)
    )
    total = count_result.scalar_one()

    unread_filters = filters + [NotificationLog.is_read.is_(False)]
    unread_result = await db.execute(
        select(func.count()).select_from(NotificationLog).where(*unread_filters)
    )
    unread_count = unread_result.scalar_one()

    result = await db.execute(
        select(NotificationLog)
        .where(*filters)
        .order_by(NotificationLog.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().all()), total, unread_count


async def create_notification(
    db: AsyncSession,
    business_id: UUID,
    notification_type: NotificationTypeEnum,
    channel: NotificationChannelEnum,
    title: str,
    body: str | None = None,
    payload_json: dict | None = None,
    user_id: UUID | None = None,
    branch_id: UUID | None = None,
    created_by: UUID | None = None,
) -> NotificationLog:
    now = _now()
    notification = NotificationLog(
        business_id=business_id,
        branch_id=branch_id,
        user_id=user_id,
        notification_type=notification_type.value,
        channel=channel.value,
        title=title,
        body=body,
        payload_json=payload_json or {},
        is_read=False,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(notification)
    await db.flush()
    return notification


async def mark_as_read(
    db: AsyncSession,
    business_id: UUID,
    notification_ids: list[UUID],
    user_id: UUID,
) -> int:
    now = _now()
    stmt = (
        update(NotificationLog)
        .where(
            NotificationLog.id.in_(notification_ids),
            NotificationLog.business_id == business_id,
            NotificationLog.deleted_at.is_(None),
            NotificationLog.is_read.is_(False),
        )
        .values(is_read=True, read_at=now, updated_at=now)
    )
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount


async def mark_all_as_read(
    db: AsyncSession,
    business_id: UUID,
    user_id: UUID,
) -> int:
    now = _now()
    stmt = (
        update(NotificationLog)
        .where(
            NotificationLog.business_id == business_id,
            NotificationLog.deleted_at.is_(None),
            NotificationLog.is_read.is_(False),
            (NotificationLog.user_id.is_(None))
            | (NotificationLog.user_id == user_id),
        )
        .values(is_read=True, read_at=now, updated_at=now)
    )
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount


async def delete_notification(
    db: AsyncSession,
    business_id: UUID,
    notification_id: UUID,
) -> None:
    result = await db.execute(
        select(NotificationLog).where(
            NotificationLog.id == notification_id,
            NotificationLog.business_id == business_id,
            NotificationLog.deleted_at.is_(None),
        )
    )
    notification = result.scalar_one_or_none()
    if notification is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )
    now = _now()
    notification.deleted_at = now
    notification.updated_at = now
    await db.commit()


async def _recent_alert_exists(
    db: AsyncSession,
    business_id: UUID,
    notification_type: NotificationTypeEnum,
    product_id: UUID,
    branch_id: UUID,
) -> bool:
    since = _now() - _ALERT_LOOKBACK
    result = await db.execute(
        text(
            """
            SELECT 1
            FROM notification_log
            WHERE business_id = :business_id
                AND deleted_at IS NULL
                AND notification_type = CAST(:notification_type AS notification_type_enum)
                AND created_at > :since
                AND payload_json->>'product_id' = :product_id
                AND payload_json->>'branch_id' = :branch_id
            LIMIT 1
            """
        ),
        {
            "business_id": business_id,
            "notification_type": notification_type.value,
            "since": since,
            "product_id": str(product_id),
            "branch_id": str(branch_id),
        },
    )
    return result.first() is not None


async def check_low_stock_alerts(
    db: AsyncSession,
    business_id: UUID,
) -> list[NotificationLog]:
    result = await db.execute(
        text(
            """
            SELECT
                pl.product_id,
                pl.branch_id,
                p.name AS product_name,
                COALESCE(stock.current_qty, 0) AS current_qty,
                pl.min_stock_level
            FROM product_locations pl
            INNER JOIN products p ON p.id = pl.product_id
            LEFT JOIN LATERAL (
                SELECT COALESCE(SUM(sm.qty), 0) AS current_qty
                FROM stock_movements sm
                WHERE sm.business_id = pl.business_id
                    AND sm.branch_id = pl.branch_id
                    AND sm.product_id = pl.product_id
                    AND sm.variation_id IS NOT DISTINCT FROM pl.variation_id
                    AND sm.deleted_at IS NULL
            ) stock ON TRUE
            WHERE pl.business_id = :business_id
                AND pl.deleted_at IS NULL
                AND pl.min_stock_level IS NOT NULL
                AND COALESCE(stock.current_qty, 0) < pl.min_stock_level
            """
        ),
        {"business_id": business_id},
    )

    created: list[NotificationLog] = []
    for row in result:
        product_id = row.product_id
        branch_id = row.branch_id
        if await _recent_alert_exists(
            db,
            business_id,
            NotificationTypeEnum.low_stock,
            product_id,
            branch_id,
        ):
            continue

        current_qty = Decimal(str(row.current_qty))
        notification = await create_notification(
            db,
            business_id,
            NotificationTypeEnum.low_stock,
            NotificationChannelEnum.in_app,
            title=f"Low Stock: {row.product_name}",
            body=(
                f"Current quantity {current_qty} is below minimum "
                f"{row.min_stock_level}"
            ),
            payload_json={
                "product_id": str(product_id),
                "branch_id": str(branch_id),
                "current_qty": str(current_qty),
                "min_stock_level": str(row.min_stock_level),
            },
            branch_id=branch_id,
        )
        created.append(notification)

    if created:
        await db.commit()
        for n in created:
            await db.refresh(n)
    return created


async def check_expiry_alerts(
    db: AsyncSession,
    business_id: UUID,
    days_before: int = 30,
) -> list[NotificationLog]:
    result = await db.execute(
        text(
            """
            SELECT DISTINCT ON (sm.product_id, sm.branch_id, sm.expiry_date)
                sm.product_id,
                sm.branch_id,
                p.name AS product_name,
                sm.expiry_date,
                COALESCE(SUM(sm.qty) OVER (
                    PARTITION BY sm.product_id, sm.branch_id, sm.expiry_date
                ), 0) AS qty
            FROM stock_movements sm
            INNER JOIN products p ON p.id = sm.product_id
            WHERE sm.business_id = :business_id
                AND sm.deleted_at IS NULL
                AND sm.expiry_date IS NOT NULL
                AND sm.expiry_date BETWEEN CURRENT_DATE
                    AND CURRENT_DATE + CAST(:days_before AS integer)
            ORDER BY sm.product_id, sm.branch_id, sm.expiry_date, sm.movement_at DESC
            """
        ),
        {"business_id": business_id, "days_before": days_before},
    )

    created: list[NotificationLog] = []
    for row in result:
        product_id = row.product_id
        branch_id = row.branch_id
        if await _recent_alert_exists(
            db,
            business_id,
            NotificationTypeEnum.expiry_warning,
            product_id,
            branch_id,
        ):
            continue

        expiry_date = row.expiry_date
        qty = Decimal(str(row.qty))
        notification = await create_notification(
            db,
            business_id,
            NotificationTypeEnum.expiry_warning,
            NotificationChannelEnum.in_app,
            title=f"Expiry Warning: {row.product_name}",
            body=f"Batch expires on {expiry_date} (qty: {qty})",
            payload_json={
                "product_id": str(product_id),
                "branch_id": str(branch_id),
                "expiry_date": expiry_date.isoformat(),
                "qty": str(qty),
            },
            branch_id=branch_id,
        )
        created.append(notification)

    if created:
        await db.commit()
        for n in created:
            await db.refresh(n)
    return created
