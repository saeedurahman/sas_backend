"""Kitchen Order Ticket (KOT) queue and status management.

Header status derivation (slowest-active-line / bottleneck rule)
----------------------------------------------------------------
After line-level updates, the KOT header status is re-derived from all
non-cancelled lines:

1. Cancelled lines are ignored.
2. If every line is cancelled (or there are no lines), header → ``cancelled``.
3. Otherwise header = the *least advanced* status among active lines, using
   priority: ``pending`` < ``preparing`` < ``ready`` < ``served``.

Examples:
- [pending, preparing] → header ``pending``
- [preparing, ready]   → header ``preparing``
- [ready, served]      → header ``ready`` (not all served yet)
- [served, served]     → header ``served``

Timestamp side effects (header ``kot_orders.ready_at`` / ``served_at``):
- Set ``ready_at`` when header first becomes ``ready`` (line sync or manual).
- Set ``served_at`` when header first becomes ``served``; also back-fills
  ``ready_at`` if it was never set.

Manual header PATCH additionally promotes all non-cancelled lines forward to
match the new header status (bulk bump for kitchen display).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import KotStatusEnum
from app.models.restaurant import KotOrder, KotOrderLine
from app.schemas.restaurant import KotOrderLineResponse, KotOrderResponse

ACTIVE_KOT_STATUSES = (
    KotStatusEnum.pending.value,
    KotStatusEnum.preparing.value,
    KotStatusEnum.ready.value,
)

KOT_STATUS_PRIORITY: dict[KotStatusEnum, int] = {
    KotStatusEnum.pending: 0,
    KotStatusEnum.preparing: 1,
    KotStatusEnum.ready: 2,
    KotStatusEnum.served: 3,
}

KOT_LINE_STATUS_TRANSITIONS: dict[KotStatusEnum, frozenset[KotStatusEnum]] = {
    KotStatusEnum.pending: frozenset(
        {KotStatusEnum.preparing, KotStatusEnum.cancelled}
    ),
    KotStatusEnum.preparing: frozenset({KotStatusEnum.ready, KotStatusEnum.cancelled}),
    KotStatusEnum.ready: frozenset({KotStatusEnum.served, KotStatusEnum.cancelled}),
    KotStatusEnum.served: frozenset(),
    KotStatusEnum.cancelled: frozenset(),
}

KOT_HEADER_STATUS_TRANSITIONS: dict[KotStatusEnum, frozenset[KotStatusEnum]] = {
    KotStatusEnum.pending: frozenset(
        {KotStatusEnum.preparing, KotStatusEnum.cancelled}
    ),
    KotStatusEnum.preparing: frozenset({KotStatusEnum.ready, KotStatusEnum.cancelled}),
    KotStatusEnum.ready: frozenset({KotStatusEnum.served, KotStatusEnum.cancelled}),
    KotStatusEnum.served: frozenset(),
    KotStatusEnum.cancelled: frozenset(),
}

_KOT_DETAIL_OPTIONS = (
    selectinload(KotOrder.lines).selectinload(KotOrderLine.product),
    selectinload(KotOrder.lines).selectinload(KotOrderLine.variation),
    selectinload(KotOrder.dining_table),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _kot_status_value(raw: str) -> KotStatusEnum:
    return KotStatusEnum(raw)


def is_valid_kot_status_transition(
    current: KotStatusEnum,
    target: KotStatusEnum,
    *,
    transitions: dict[KotStatusEnum, frozenset[KotStatusEnum]],
) -> bool:
    if current == target:
        return True
    return target in transitions.get(current, frozenset())


def derive_kot_header_status(line_statuses: list[str]) -> KotStatusEnum:
    """Slowest-active-line rule — see module docstring."""
    active = [
        _kot_status_value(s)
        for s in line_statuses
        if s != KotStatusEnum.cancelled.value
    ]
    if not active:
        return KotStatusEnum.cancelled

    slowest = min(active, key=lambda s: KOT_STATUS_PRIORITY[s])
    return slowest


def _apply_header_timestamps(
    order: KotOrder,
    header_status: KotStatusEnum,
    now: datetime,
) -> None:
    if header_status in (KotStatusEnum.ready, KotStatusEnum.served):
        if order.ready_at is None:
            order.ready_at = now
    if header_status == KotStatusEnum.served and order.served_at is None:
        order.served_at = now


def _promote_lines_to_status(
    order: KotOrder,
    target: KotStatusEnum,
    now: datetime,
) -> None:
    """Bulk-promote non-cancelled lines when header is manually advanced."""
    target_priority = KOT_STATUS_PRIORITY[target]
    for line in order.lines:
        line_status = _kot_status_value(line.status)
        if line_status == KotStatusEnum.cancelled:
            continue
        if KOT_STATUS_PRIORITY[line_status] < target_priority:
            line.status = target.value
            line.updated_at = now


def sync_kot_order_status_from_lines(order: KotOrder, now: datetime) -> None:
    derived = derive_kot_header_status([line.status for line in order.lines])
    order.status = derived.value
    _apply_header_timestamps(order, derived, now)


def _build_kot_response(order: KotOrder) -> KotOrderResponse:
    table_number = (
        order.dining_table.table_number if order.dining_table is not None else None
    )
    lines = [
        KotOrderLineResponse(
            id=line.id,
            kot_order_id=line.kot_order_id,
            sale_line_id=line.sale_line_id,
            product_id=line.product_id,
            product_name=line.product.name,
            variation_id=line.variation_id,
            qty=line.qty,
            modifiers_json=line.modifiers_json or [],
            kitchen_notes=line.kitchen_notes,
            status=line.status,
        )
        for line in order.lines
    ]
    return KotOrderResponse(
        id=order.id,
        business_id=order.business_id,
        branch_id=order.branch_id,
        sale_id=order.sale_id,
        table_id=order.table_id,
        table_number=table_number,
        kot_number=order.kot_number,
        status=order.status,
        fired_at=order.fired_at,
        ready_at=order.ready_at,
        served_at=order.served_at,
        notes=order.notes,
        lines=lines,
    )


async def _get_kot_order_for_update(
    db: AsyncSession,
    kot_id: UUID,
    business_id: UUID,
) -> KotOrder:
    result = await db.execute(
        select(KotOrder)
        .where(
            KotOrder.id == kot_id,
            KotOrder.business_id == business_id,
            KotOrder.deleted_at.is_(None),
        )
        .options(*_KOT_DETAIL_OPTIONS)
        .with_for_update()
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="KOT order not found",
        )
    return order


async def get_active_kot_orders(
    db: AsyncSession,
    business_id: UUID,
    *,
    branch_id: UUID | None = None,
) -> list[KotOrderResponse]:
    stmt = (
        select(KotOrder)
        .where(
            KotOrder.business_id == business_id,
            KotOrder.deleted_at.is_(None),
            KotOrder.status.in_(ACTIVE_KOT_STATUSES),
        )
        .options(*_KOT_DETAIL_OPTIONS)
        .order_by(KotOrder.fired_at.asc())
    )
    if branch_id is not None:
        stmt = stmt.where(KotOrder.branch_id == branch_id)

    result = await db.execute(stmt)
    return [_build_kot_response(order) for order in result.scalars().all()]


async def get_kot_order_by_id(
    db: AsyncSession,
    kot_id: UUID,
    business_id: UUID,
) -> KotOrderResponse:
    result = await db.execute(
        select(KotOrder)
        .where(
            KotOrder.id == kot_id,
            KotOrder.business_id == business_id,
            KotOrder.deleted_at.is_(None),
        )
        .options(*_KOT_DETAIL_OPTIONS)
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="KOT order not found",
        )
    return _build_kot_response(order)


async def get_kot_orders_by_table(
    db: AsyncSession,
    table_id: UUID,
    business_id: UUID,
) -> list[KotOrderResponse]:
    result = await db.execute(
        select(KotOrder)
        .where(
            KotOrder.business_id == business_id,
            KotOrder.table_id == table_id,
            KotOrder.deleted_at.is_(None),
            KotOrder.status.in_(ACTIVE_KOT_STATUSES),
        )
        .options(*_KOT_DETAIL_OPTIONS)
        .order_by(KotOrder.fired_at.asc())
    )
    return [_build_kot_response(order) for order in result.scalars().all()]


async def update_kot_order_status(
    db: AsyncSession,
    kot_id: UUID,
    business_id: UUID,
    new_status: KotStatusEnum,
    updated_by: UUID,
) -> KotOrderResponse:
    try:
        order = await _get_kot_order_for_update(db, kot_id, business_id)
        current = _kot_status_value(order.status)
        if not is_valid_kot_status_transition(
            current, new_status, transitions=KOT_HEADER_STATUS_TRANSITIONS
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid KOT status transition from '{current.value}' "
                    f"to '{new_status.value}'"
                ),
            )

        now = _now()
        order.status = new_status.value
        order.updated_by = updated_by
        order.updated_at = now

        if new_status == KotStatusEnum.cancelled:
            for line in order.lines:
                if line.status != KotStatusEnum.cancelled.value:
                    line.status = KotStatusEnum.cancelled.value
                    line.updated_at = now
        elif new_status != KotStatusEnum.pending:
            _promote_lines_to_status(order, new_status, now)

        _apply_header_timestamps(order, new_status, now)
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_kot_order_by_id(db, kot_id, business_id)


async def update_kot_order_line_status(
    db: AsyncSession,
    line_id: UUID,
    business_id: UUID,
    new_status: KotStatusEnum,
    updated_by: UUID,
) -> KotOrderResponse:
    try:
        result = await db.execute(
            select(KotOrderLine)
            .where(
                KotOrderLine.id == line_id,
                KotOrderLine.business_id == business_id,
            )
            .options(
                selectinload(KotOrderLine.kot_order)
                .selectinload(KotOrder.lines)
                .selectinload(KotOrderLine.product),
                selectinload(KotOrderLine.kot_order)
                .selectinload(KotOrder.lines)
                .selectinload(KotOrderLine.variation),
                selectinload(KotOrderLine.kot_order).selectinload(
                    KotOrder.dining_table
                ),
            )
            .with_for_update()
        )
        line = result.scalar_one_or_none()
        if line is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="KOT line not found",
            )

        order = line.kot_order
        if order.deleted_at is not None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="KOT order not found",
            )

        current = _kot_status_value(line.status)
        if not is_valid_kot_status_transition(
            current, new_status, transitions=KOT_LINE_STATUS_TRANSITIONS
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid KOT line status transition from '{current.value}' "
                    f"to '{new_status.value}'"
                ),
            )

        now = _now()
        line.status = new_status.value
        line.updated_at = now
        order.updated_by = updated_by
        order.updated_at = now
        sync_kot_order_status_from_lines(order, now)

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_kot_order_by_id(db, order.id, business_id)
