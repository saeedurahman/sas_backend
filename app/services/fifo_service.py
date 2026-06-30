"""FIFO layer consumption and restoration for sales, returns, and cancellations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import (
    NotificationChannelEnum,
    NotificationTypeEnum,
    ReferenceTypeEnum,
    StockMovementTypeEnum,
)
from app.models.inventory import PurchaseLine, StockMovement
from app.models.product import Product
from app.services.invoice_service import _round2
from app.services.notification_service import create_notification
from app.services.stock_service import create_stock_movement, verify_product

_ZERO = Decimal("0")
_ALERT_LOOKBACK = timedelta(hours=24)


@dataclass(frozen=True)
class FifoLayerConsumption:
    purchase_line_id: UUID | None
    qty: Decimal
    cost_per_unit: Decimal


def compute_weighted_cost_per_unit(
    total_qty: Decimal,
    slices: list[FifoLayerConsumption],
) -> Decimal:
    if total_qty <= _ZERO:
        return _ZERO
    total_cost = sum((s.qty * s.cost_per_unit for s in slices), _ZERO)
    return _round2(total_cost / total_qty)


async def get_fallback_cost_per_unit(
    db: AsyncSession,
    business_id: UUID,
    product_id: UUID,
    variation_id: UUID | None,
) -> Decimal:
    stmt = (
        select(PurchaseLine.cost_per_unit)
        .where(
            PurchaseLine.business_id == business_id,
            PurchaseLine.product_id == product_id,
            PurchaseLine.deleted_at.is_(None),
        )
        .order_by(PurchaseLine.created_at.desc())
        .limit(1)
    )
    if variation_id is None:
        stmt = stmt.where(PurchaseLine.variation_id.is_(None))
    else:
        stmt = stmt.where(PurchaseLine.variation_id == variation_id)

    last_cost = (await db.execute(stmt)).scalar_one_or_none()
    if last_cost is not None:
        return Decimal(str(last_cost))
    return _ZERO


async def consume_fifo_layers(
    db: AsyncSession,
    business_id: UUID,
    product_id: UUID,
    variation_id: UUID | None,
    qty: Decimal,
) -> tuple[list[FifoLayerConsumption], Decimal]:
    """Consume oldest layers first; return layer slices and any uncovered shortfall."""
    if qty <= _ZERO:
        return [], _ZERO

    filters = [
        PurchaseLine.business_id == business_id,
        PurchaseLine.product_id == product_id,
        PurchaseLine.deleted_at.is_(None),
        PurchaseLine.qty_remaining > _ZERO,
    ]
    if variation_id is None:
        filters.append(PurchaseLine.variation_id.is_(None))
    else:
        filters.append(PurchaseLine.variation_id == variation_id)

    result = await db.execute(
        select(PurchaseLine)
        .where(*filters)
        .order_by(
            PurchaseLine.updated_at.asc(),
            PurchaseLine.created_at.asc(),
            PurchaseLine.id.asc(),
        )
        .with_for_update()
    )
    layers = list(result.scalars().all())

    now = datetime.now(timezone.utc)
    remaining = qty
    consumptions: list[FifoLayerConsumption] = []

    for layer in layers:
        if remaining <= _ZERO:
            break
        take = min(remaining, layer.qty_remaining)
        if take <= _ZERO:
            continue
        layer.qty_remaining -= take
        layer.updated_at = now
        consumptions.append(
            FifoLayerConsumption(
                purchase_line_id=layer.id,
                qty=take,
                cost_per_unit=Decimal(str(layer.cost_per_unit)),
            )
        )
        remaining -= take

    return consumptions, remaining


async def restore_fifo_layers(
    db: AsyncSession,
    business_id: UUID,
    consumptions: list[FifoLayerConsumption],
) -> None:
    """Increment qty_remaining on the same purchase layers (capped at received_qty)."""
    if not consumptions:
        return

    layer_ids = [
        c.purchase_line_id for c in consumptions if c.purchase_line_id is not None
    ]
    if not layer_ids:
        return

    result = await db.execute(
        select(PurchaseLine)
        .where(
            PurchaseLine.business_id == business_id,
            PurchaseLine.id.in_(layer_ids),
            PurchaseLine.deleted_at.is_(None),
        )
        .with_for_update()
    )
    by_id = {layer.id: layer for layer in result.scalars().all()}
    now = datetime.now(timezone.utc)

    for consumption in consumptions:
        if consumption.purchase_line_id is None:
            continue
        layer = by_id.get(consumption.purchase_line_id)
        if layer is None:
            continue
        restored = layer.qty_remaining + consumption.qty
        if restored > layer.received_qty:
            restored = layer.received_qty
        layer.qty_remaining = restored
        layer.updated_at = now


async def get_traced_sale_line_consumptions(
    db: AsyncSession,
    business_id: UUID,
    sale_line_id: UUID,
) -> list[FifoLayerConsumption]:
    result = await db.execute(
        select(StockMovement)
        .where(
            StockMovement.business_id == business_id,
            StockMovement.movement_type == StockMovementTypeEnum.sale.value,
            StockMovement.reference_type == ReferenceTypeEnum.sale_line.value,
            StockMovement.reference_id == sale_line_id,
            StockMovement.purchase_line_id.isnot(None),
            StockMovement.deleted_at.is_(None),
        )
        .order_by(
            StockMovement.movement_sequence.asc().nulls_last(),
            StockMovement.movement_at.asc(),
            StockMovement.created_at.asc(),
            StockMovement.id.asc(),
        )
    )
    consumptions: list[FifoLayerConsumption] = []
    for movement in result.scalars().all():
        qty = abs(Decimal(str(movement.qty)))
        if qty <= _ZERO:
            continue
        consumptions.append(
            FifoLayerConsumption(
                purchase_line_id=movement.purchase_line_id,
                qty=qty,
                cost_per_unit=Decimal(str(movement.cost_per_unit)),
            )
        )
    return consumptions


async def get_untraced_sale_line_qty(
    db: AsyncSession,
    business_id: UUID,
    sale_line_id: UUID,
) -> Decimal:
    result = await db.execute(
        select(StockMovement)
        .where(
            StockMovement.business_id == business_id,
            StockMovement.movement_type == StockMovementTypeEnum.sale.value,
            StockMovement.reference_type == ReferenceTypeEnum.sale_line.value,
            StockMovement.reference_id == sale_line_id,
            StockMovement.purchase_line_id.is_(None),
            StockMovement.deleted_at.is_(None),
        )
    )
    total = _ZERO
    for movement in result.scalars().all():
        total += abs(Decimal(str(movement.qty)))
    return total


def plan_lifo_restoration(
    traced_consumptions: list[FifoLayerConsumption],
    qty_to_restore: Decimal,
) -> list[FifoLayerConsumption]:
    """Restore newest consumed layers first (LIFO unwind)."""
    if qty_to_restore <= _ZERO or not traced_consumptions:
        return []

    remaining = qty_to_restore
    plan: list[FifoLayerConsumption] = []
    for consumption in reversed(traced_consumptions):
        if remaining <= _ZERO:
            break
        restore_qty = min(remaining, consumption.qty)
        if restore_qty <= _ZERO:
            continue
        plan.append(
            FifoLayerConsumption(
                purchase_line_id=consumption.purchase_line_id,
                qty=restore_qty,
                cost_per_unit=consumption.cost_per_unit,
            )
        )
        remaining -= restore_qty
    return plan


async def allocate_sale_line_fifo(
    db: AsyncSession,
    business_id: UUID,
    product_id: UUID,
    variation_id: UUID | None,
    qty: Decimal,
) -> tuple[Decimal, list[FifoLayerConsumption], Decimal]:
    """Layer consumption + optional fallback slice; returns weighted unit cost."""
    layer_consumptions, short_qty = await consume_fifo_layers(
        db, business_id, product_id, variation_id, qty
    )
    slices = list(layer_consumptions)
    if short_qty > _ZERO:
        fallback = await get_fallback_cost_per_unit(
            db, business_id, product_id, variation_id
        )
        slices.append(
            FifoLayerConsumption(
                purchase_line_id=None,
                qty=short_qty,
                cost_per_unit=fallback,
            )
        )
    weighted = compute_weighted_cost_per_unit(qty, slices)
    return weighted, slices, short_qty


async def create_sale_line_stock_movements(
    db: AsyncSession,
    *,
    business_id: UUID,
    branch_id: UUID,
    product_id: UUID,
    variation_id: UUID | None,
    sale_line_id: UUID,
    consumptions: list[FifoLayerConsumption],
    created_by: UUID,
    movement_at: datetime,
) -> None:
    for index, consumption in enumerate(consumptions):
        await create_stock_movement(
            db=db,
            business_id=business_id,
            branch_id=branch_id,
            product_id=product_id,
            variation_id=variation_id,
            movement_type=StockMovementTypeEnum.sale,
            qty=-consumption.qty,
            cost_per_unit=consumption.cost_per_unit,
            reference_type=ReferenceTypeEnum.sale_line,
            reference_id=sale_line_id,
            created_by=created_by,
            purchase_line_id=consumption.purchase_line_id,
            movement_at=movement_at,
            movement_sequence=index,
        )


async def restore_sale_line_inventory(
    db: AsyncSession,
    *,
    business_id: UUID,
    branch_id: UUID,
    product_id: UUID,
    variation_id: UUID | None,
    sale_line_id: UUID,
    sale_line_qty: Decimal,
    sale_line_cost_per_unit: Decimal,
    qty_to_restore: Decimal,
    movement_type: StockMovementTypeEnum,
    reference_type: ReferenceTypeEnum,
    reference_id: UUID,
    created_by: UUID,
    movement_at: datetime,
) -> None:
    traced = await get_traced_sale_line_consumptions(
        db, business_id, sale_line_id
    )
    if not traced:
        await create_stock_movement(
            db=db,
            business_id=business_id,
            branch_id=branch_id,
            product_id=product_id,
            variation_id=variation_id,
            movement_type=movement_type,
            qty=qty_to_restore,
            cost_per_unit=sale_line_cost_per_unit,
            reference_type=reference_type,
            reference_id=reference_id,
            created_by=created_by,
            movement_at=movement_at,
        )
        return

    traced_total = sum((c.qty for c in traced), _ZERO)
    untraced_total = await get_untraced_sale_line_qty(
        db, business_id, sale_line_id
    )
    if traced_total + untraced_total != sale_line_qty:
        untraced_total = max(sale_line_qty - traced_total, _ZERO)

    remaining = qty_to_restore
    restore_plan = plan_lifo_restoration(
        traced, min(remaining, traced_total)
    )
    await restore_fifo_layers(db, business_id, restore_plan)

    for consumption in restore_plan:
        await create_stock_movement(
            db=db,
            business_id=business_id,
            branch_id=branch_id,
            product_id=product_id,
            variation_id=variation_id,
            movement_type=movement_type,
            qty=consumption.qty,
            cost_per_unit=consumption.cost_per_unit,
            reference_type=reference_type,
            reference_id=reference_id,
            created_by=created_by,
            purchase_line_id=consumption.purchase_line_id,
            movement_at=movement_at,
        )
    remaining -= sum((c.qty for c in restore_plan), _ZERO)

    if remaining > _ZERO and untraced_total > _ZERO:
        legacy_qty = min(remaining, untraced_total)
        await create_stock_movement(
            db=db,
            business_id=business_id,
            branch_id=branch_id,
            product_id=product_id,
            variation_id=variation_id,
            movement_type=movement_type,
            qty=legacy_qty,
            cost_per_unit=sale_line_cost_per_unit,
            reference_type=reference_type,
            reference_id=reference_id,
            created_by=created_by,
            movement_at=movement_at,
        )


async def _recent_fifo_alert_exists(
    db: AsyncSession,
    business_id: UUID,
    product_id: UUID,
    variation_id: UUID | None,
    branch_id: UUID,
) -> bool:
    since = datetime.now(timezone.utc) - _ALERT_LOOKBACK
    variation_key = str(variation_id) if variation_id is not None else ""
    result = await db.execute(
        text(
            """
            SELECT 1
            FROM notification_log
            WHERE business_id = :business_id
              AND deleted_at IS NULL
              AND notification_type = CAST(:notification_type AS notification_type_enum)
              AND created_at > :since
              AND payload_json->>'alert_kind' = 'fifo_insufficient_layers'
              AND payload_json->>'product_id' = :product_id
              AND payload_json->>'branch_id' = :branch_id
              AND COALESCE(payload_json->>'variation_id', '') = :variation_id
            LIMIT 1
            """
        ),
        {
            "business_id": business_id,
            "notification_type": NotificationTypeEnum.system.value,
            "since": since,
            "product_id": str(product_id),
            "branch_id": str(branch_id),
            "variation_id": variation_key,
        },
    )
    return result.first() is not None


async def maybe_notify_fifo_shortfall(
    db: AsyncSession,
    *,
    business_id: UUID,
    branch_id: UUID,
    product_id: UUID,
    variation_id: UUID | None,
    sale_line_id: UUID,
    requested_qty: Decimal,
    consumed_from_layers_qty: Decimal,
    short_qty: Decimal,
    fallback_cost_per_unit: Decimal,
    created_by: UUID | None,
) -> None:
    if short_qty <= _ZERO:
        return
    if await _recent_fifo_alert_exists(
        db, business_id, product_id, variation_id, branch_id
    ):
        return

    product = await verify_product(db, product_id, business_id)
    product_name = product.name
    await create_notification(
        db,
        business_id,
        NotificationTypeEnum.system,
        NotificationChannelEnum.in_app,
        title=f"FIFO cost incomplete: {product_name}",
        body=(
            f"Sale consumed {consumed_from_layers_qty} unit(s) from FIFO layers but "
            f"{short_qty} unit(s) used fallback cost Rs. {fallback_cost_per_unit}."
        ),
        payload_json={
            "alert_kind": "fifo_insufficient_layers",
            "product_id": str(product_id),
            "variation_id": str(variation_id) if variation_id else None,
            "branch_id": str(branch_id),
            "sale_line_id": str(sale_line_id),
            "requested_qty": str(requested_qty),
            "consumed_from_layers_qty": str(consumed_from_layers_qty),
            "short_qty": str(short_qty),
            "fallback_cost_per_unit": str(fallback_cost_per_unit),
        },
        branch_id=branch_id,
        created_by=created_by,
    )


async def get_fifo_cost_from_db(
    db: AsyncSession,
    business_id: UUID,
    product_id: UUID,
    variation_id: UUID | None,
    qty: Decimal,
) -> Decimal | None:
    """Read-only weighted FIFO cost via existing SQL function (for tests/comparison)."""
    from sqlalchemy import text

    try:
        async with db.begin_nested():
            result = await db.execute(
                text("SELECT get_fifo_cost(:bid, :pid, :vid, :qty)"),
                {
                    "bid": business_id,
                    "pid": product_id,
                    "vid": variation_id,
                    "qty": qty,
                },
            )
            cost = result.scalar_one_or_none()
        if cost is None:
            return None
        return Decimal(str(cost))
    except Exception:
        return None
