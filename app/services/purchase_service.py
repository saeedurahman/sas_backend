"""Purchase order and receipt services."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import (
    LedgerEntryTypeEnum,
    PurchaseOrderStatusEnum,
    ReferenceTypeEnum,
    StockMovementTypeEnum,
)
from app.models.inventory import (
    PurchaseLine,
    PurchaseOrder,
    PurchaseReceipt,
    PurchaseReceiptLine,
)
from app.schemas.inventory import CreatePurchaseOrderRequest, CreatePurchaseReceiptRequest
from app.services.stock_service import (
    _now,
    generate_document_number,
    verify_branch,
    verify_product,
    verify_variation,
)
from app.services.fifo_service import _round4, create_fifo_layer
from app.services.stock_service import create_stock_movement
from app.services.supplier_ledger_service import create_supplier_ledger_entry
from app.services.supplier_service import get_supplier_by_id


def _po_load_options():
    return (
        selectinload(PurchaseOrder.supplier),
        selectinload(PurchaseOrder.lines),
    )


def _po_detail_options():
    return (
        selectinload(PurchaseOrder.supplier),
        selectinload(PurchaseOrder.lines),
        selectinload(PurchaseOrder.receipts),
    )


async def get_purchase_orders(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
    supplier_id: UUID | None = None,
    status: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[PurchaseOrder], int]:
    filters = [
        PurchaseOrder.business_id == business_id,
        PurchaseOrder.deleted_at.is_(None),
    ]
    if branch_id is not None:
        filters.append(PurchaseOrder.branch_id == branch_id)
    if supplier_id is not None:
        filters.append(PurchaseOrder.supplier_id == supplier_id)
    if status is not None:
        filters.append(PurchaseOrder.status == status)

    count_result = await db.execute(
        select(func.count()).select_from(PurchaseOrder).where(*filters)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(PurchaseOrder)
        .where(*filters)
        .options(*_po_load_options())
        .order_by(PurchaseOrder.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().unique().all()), total


async def get_purchase_order_by_id(
    db: AsyncSession,
    po_id: UUID,
    business_id: UUID,
) -> PurchaseOrder:
    result = await db.execute(
        select(PurchaseOrder)
        .where(
            PurchaseOrder.id == po_id,
            PurchaseOrder.business_id == business_id,
            PurchaseOrder.deleted_at.is_(None),
        )
        .options(*_po_detail_options())
    )
    po = result.scalar_one_or_none()
    if po is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Purchase order not found",
        )
    return po


async def create_purchase_order(
    db: AsyncSession,
    business_id: UUID,
    data: CreatePurchaseOrderRequest,
    created_by: UUID,
) -> PurchaseOrder:
    now = _now()

    try:
        await get_supplier_by_id(db, data.supplier_id, business_id)
        await verify_branch(db, data.branch_id, business_id)

        for line in data.lines:
            await verify_product(db, line.product_id, business_id)
            if line.variation_id is not None:
                await verify_variation(
                    db, line.variation_id, line.product_id, business_id
                )

        po_number = await generate_document_number(
            db, business_id, "PO", PurchaseOrder, PurchaseOrder.po_number
        )

        po = PurchaseOrder(
            business_id=business_id,
            branch_id=data.branch_id,
            supplier_id=data.supplier_id,
            po_number=po_number,
            status="draft",
            expected_at=data.expected_at,
            notes=data.notes,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(po)
        await db.flush()

        for line_data in data.lines:
            purchase_line = PurchaseLine(
                business_id=business_id,
                purchase_order_id=po.id,
                product_id=line_data.product_id,
                variation_id=line_data.variation_id,
                branch_id=data.branch_id,
                layer_source="purchase",
                ordered_qty=line_data.ordered_qty,
                received_qty=Decimal("0"),
                qty_remaining=Decimal("0"),
                cost_per_unit=line_data.cost_per_unit,
                tax_rate=line_data.tax_rate,
                batch_number=line_data.batch_number,
                expiry_date=line_data.expiry_date,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            db.add(purchase_line)

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_purchase_order_by_id(db, po.id, business_id)


_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"ordered", "cancelled"},
    "ordered": {"cancelled"},
}


async def update_purchase_order_status(
    db: AsyncSession,
    po_id: UUID,
    business_id: UUID,
    new_status: PurchaseOrderStatusEnum,
    updated_by: UUID,
) -> PurchaseOrder:
    po = await get_purchase_order_by_id(db, po_id, business_id)
    current = po.status
    target = new_status.value

    if current == "received":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot change status of a received purchase order",
        )

    if current in ("partial", "received"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status transition from {current} to {target}",
        )

    allowed = _ALLOWED_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status transition from {current} to {target}",
        )

    now = _now()
    po.status = target
    po.updated_by = updated_by
    po.updated_at = now
    if target == "ordered" and po.ordered_at is None:
        po.ordered_at = now

    await db.commit()
    return await get_purchase_order_by_id(db, po_id, business_id)


async def _update_po_status_from_receipt(
    db: AsyncSession,
    po: PurchaseOrder,
    updated_by: UUID,
) -> None:
    lines_result = await db.execute(
        select(PurchaseLine).where(
            PurchaseLine.purchase_order_id == po.id,
            PurchaseLine.business_id == po.business_id,
            PurchaseLine.deleted_at.is_(None),
        )
    )
    lines = list(lines_result.scalars().all())
    if not lines:
        return

    all_received = all(
        line.received_qty >= line.ordered_qty for line in lines
    )
    now = _now()
    po.status = "received" if all_received else "partial"
    po.updated_by = updated_by
    po.updated_at = now


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


async def create_purchase_receipt(
    db: AsyncSession,
    business_id: UUID,
    data: CreatePurchaseReceiptRequest,
    created_by: UUID,
) -> PurchaseReceipt:
    now = _now()
    received_at = data.received_at or now

    try:
        await verify_branch(db, data.branch_id, business_id)
        await get_supplier_by_id(db, data.supplier_id, business_id)

        po: PurchaseOrder | None = None
        if data.purchase_order_id is not None:
            # Serialize receipts against one PO: status roll-up and over-receipt
            # caps are computed from rows this transaction now owns.
            await db.execute(
                select(PurchaseOrder.id)
                .where(
                    PurchaseOrder.id == data.purchase_order_id,
                    PurchaseOrder.business_id == business_id,
                )
                .with_for_update()
            )
            po = await get_purchase_order_by_id(
                db, data.purchase_order_id, business_id
            )
            if po.status == "cancelled":
                raise _bad_request(
                    "Cannot receive goods against a cancelled purchase order"
                )
            if po.supplier_id != data.supplier_id:
                raise _bad_request("supplier_id does not match the purchase order")
            if po.branch_id != data.branch_id:
                raise _bad_request("branch_id does not match the purchase order")

        # Lock referenced PO lines in id order (no deadlocks between receipts).
        po_lines: dict[UUID, PurchaseLine] = {}
        line_ids = sorted(
            {l.purchase_line_id for l in data.lines if l.purchase_line_id is not None}
        )
        if line_ids:
            if po is None:
                raise _bad_request(
                    "purchase_order_id is required when purchase_line_id is given"
                )
            locked = await db.execute(
                select(PurchaseLine)
                .where(
                    PurchaseLine.id.in_(line_ids),
                    PurchaseLine.business_id == business_id,
                    PurchaseLine.purchase_order_id == po.id,
                    PurchaseLine.deleted_at.is_(None),
                )
                .order_by(PurchaseLine.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            po_lines = {l.id: l for l in locked.scalars().all()}
            if any(i not in po_lines for i in line_ids):
                raise _bad_request(
                    "Purchase line does not belong to this purchase order"
                )

        pending_by_line: dict[UUID, Decimal] = {}
        for line in data.lines:
            await verify_product(db, line.product_id, business_id)
            if line.variation_id is not None:
                await verify_variation(
                    db, line.variation_id, line.product_id, business_id
                )
            if line.purchase_line_id is not None:
                pl = po_lines[line.purchase_line_id]
                if pl.product_id != line.product_id or pl.variation_id != line.variation_id:
                    raise _bad_request(
                        "Receipt line product/variation does not match the purchase line"
                    )
                total_in = (
                    pl.received_qty
                    + pending_by_line.get(pl.id, Decimal("0"))
                    + line.qty_received
                )
                if total_in > pl.ordered_qty:
                    raise _bad_request(
                        f"Over-receipt: ordered {pl.ordered_qty}, already received "
                        f"{pl.received_qty}, this receipt adds {line.qty_received}"
                    )
                pending_by_line[pl.id] = (
                    pending_by_line.get(pl.id, Decimal("0")) + line.qty_received
                )

        receipt_number = await generate_document_number(
            db,
            business_id,
            "GRN",
            PurchaseReceipt,
            PurchaseReceipt.receipt_number,
        )

        receipt = PurchaseReceipt(
            business_id=business_id,
            branch_id=data.branch_id,
            purchase_order_id=data.purchase_order_id,
            supplier_id=data.supplier_id,
            receipt_number=receipt_number,
            received_at=received_at,
            supplier_invoice_no=data.supplier_invoice_no,
            notes=data.notes,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(receipt)
        await db.flush()

        for line_data in data.lines:
            receipt_line = PurchaseReceiptLine(
                business_id=business_id,
                purchase_receipt_id=receipt.id,
                purchase_line_id=line_data.purchase_line_id,
                product_id=line_data.product_id,
                variation_id=line_data.variation_id,
                qty_received=line_data.qty_received,
                cost_per_unit=line_data.cost_per_unit,
                batch_number=line_data.batch_number,
                expiry_date=line_data.expiry_date,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            db.add(receipt_line)
            await db.flush()

            cost = _round4(line_data.cost_per_unit)
            layer: PurchaseLine | None = None
            if line_data.purchase_line_id is not None:
                purchase_line = po_lines[line_data.purchase_line_id]
                first_receipt = purchase_line.received_qty == 0
                purchase_line.received_qty += line_data.qty_received
                purchase_line.updated_by = created_by
                purchase_line.updated_at = now
                # The PO line doubles as the cost layer while the receipt is at
                # its cost; a different invoiced cost needs a layer of its own.
                if _round4(purchase_line.cost_per_unit) == cost:
                    layer = purchase_line
                    layer.qty_remaining += line_data.qty_received
                    layer.branch_id = data.branch_id
                    if first_receipt:
                        # Layer age is when the goods arrived, not when the PO was typed.
                        layer.created_at = received_at
                    if line_data.batch_number is not None:
                        layer.batch_number = line_data.batch_number
                    if line_data.expiry_date is not None:
                        layer.expiry_date = line_data.expiry_date
            if layer is None:
                layer = await create_fifo_layer(
                    db,
                    business_id=business_id,
                    branch_id=data.branch_id,
                    product_id=line_data.product_id,
                    variation_id=line_data.variation_id,
                    qty=line_data.qty_received,
                    cost_per_unit=cost,
                    created_by=created_by,
                    source="receipt",
                    created_at=received_at,
                    batch_number=line_data.batch_number,
                    expiry_date=line_data.expiry_date,
                )

            await create_stock_movement(
                db=db,
                business_id=business_id,
                branch_id=data.branch_id,
                product_id=line_data.product_id,
                variation_id=line_data.variation_id,
                movement_type=StockMovementTypeEnum.purchase,
                qty=line_data.qty_received,
                cost_per_unit=cost,
                reference_type=ReferenceTypeEnum.purchase_receipt_line,
                reference_id=receipt_line.id,
                created_by=created_by,
                purchase_line_id=layer.id,
                batch_number=line_data.batch_number,
                expiry_date=line_data.expiry_date,
                movement_at=received_at,
            )

            # We now owe the supplier for what arrived (negative = payable).
            value = (line_data.qty_received * cost).quantize(Decimal("0.01"))
            if value > 0:
                await create_supplier_ledger_entry(
                    db,
                    business_id,
                    data.supplier_id,
                    LedgerEntryTypeEnum.sale,
                    -value,
                    reference_type=ReferenceTypeEnum.purchase_receipt_line.value,
                    reference_id=receipt_line.id,
                    notes=f"Goods receipt {receipt_number}",
                    created_by=created_by,
                )

        if po is not None:
            await _update_po_status_from_receipt(db, po, created_by)

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return await get_purchase_receipt_by_id(db, receipt.id, business_id)


async def get_purchase_receipt_by_id(
    db: AsyncSession,
    receipt_id: UUID,
    business_id: UUID,
) -> PurchaseReceipt:
    result = await db.execute(
        select(PurchaseReceipt)
        .where(
            PurchaseReceipt.id == receipt_id,
            PurchaseReceipt.business_id == business_id,
            PurchaseReceipt.deleted_at.is_(None),
        )
        .options(
            selectinload(PurchaseReceipt.supplier),
            selectinload(PurchaseReceipt.lines),
        )
    )
    receipt = result.scalar_one_or_none()
    if receipt is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Purchase receipt not found",
        )
    return receipt
