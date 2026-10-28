"""Dine-in tab lifecycle: open, add lines, fire to kitchen, bill, complete."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.business import BusinessConfig
from app.models.enums import (
    KotStatusEnum,
    LedgerEntryTypeEnum,
    PaymentMethodEnum,
    ReferenceTypeEnum,
    RegisterTxTypeEnum,
    SaleStatusEnum,
    SaleTypeEnum,
    TableStatusEnum,
)
from app.models.register import RegisterTransaction
from app.models.restaurant import KotOrder, KotOrderLine
from app.models.sales import Sale, SaleLine, SalePayment
from app.schemas.sales import (
    AddSaleLinesRequest,
    CompleteTabRequest,
    CreateSaleRequest,
    OpenTabRequest,
    TabSaleLineRequest,
)
from app.services.customer_service import create_ledger_entry, get_customer_by_id
from app.services.discount_engine import merge_line_discount_inputs
from app.services.feature_flags import get_feature_flag
from app.services.fifo_service import (
    allocate_sale_line_fifo,
    create_sale_line_stock_movements,
    maybe_notify_fifo_shortfall,
)
from app.services.invoice_service import _round2
from app.services.pricing_engine import calculate_line_total, determine_sale_status
from app.services.register_service import verify_open_register_shift
from app.services.restaurant_modifier_service import validate_line_modifiers
from app.services.restaurant_table_service import (
    get_dining_table_by_id,
    transition_table_status,
)
from app.services.stock_service import (
    _now,
    check_sufficient_stock,
    generate_document_number,
    get_allow_negative_stock,
    verify_branch,
    verify_product,
    verify_variation,
)

_LINE_NOTES_PREFIX = "restaurant:"
ACTIVE_TAB_STATUSES = (
    SaleStatusEnum.held.value,
    SaleStatusEnum.draft.value,
    SaleStatusEnum.partially_paid.value,
)


def _encode_sale_line_notes(
    kitchen_notes: str | None,
    modifiers: list[dict[str, Any]],
) -> str | None:
    if modifiers:
        payload = {
            "kitchen_notes": kitchen_notes,
            "modifiers": [
                {
                    "modifier_id": str(item["modifier_id"]),
                    "name": item["name"],
                    "price_delta": str(item["price_delta"]),
                }
                for item in modifiers
            ],
        }
        return f"{_LINE_NOTES_PREFIX}{json.dumps(payload)}"
    return kitchen_notes


def decode_sale_line_notes(notes: str | None) -> tuple[str | None, list[dict[str, Any]]]:
    if notes and notes.startswith(_LINE_NOTES_PREFIX):
        payload = json.loads(notes[len(_LINE_NOTES_PREFIX) :])
        modifiers: list[dict[str, Any]] = []
        for item in payload.get("modifiers", []):
            modifiers.append(
                {
                    "modifier_id": UUID(str(item["modifier_id"])),
                    "name": item["name"],
                    "price_delta": Decimal(str(item["price_delta"])),
                }
            )
        return payload.get("kitchen_notes"), modifiers
    return notes, []


async def _get_business_config(
    db: AsyncSession,
    business_id: UUID,
) -> BusinessConfig:
    result = await db.execute(
        select(BusinessConfig).where(
            BusinessConfig.business_id == business_id,
            BusinessConfig.deleted_at.is_(None),
        )
    )
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Business configuration not found",
        )
    return config


def require_tab_feature_flags(config: BusinessConfig, *flag_keys: str) -> None:
    for flag_key in flag_keys:
        if not get_feature_flag(config.config_json, flag_key):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Feature not enabled: {flag_key}",
            )


async def _count_active_tabs_on_table(
    db: AsyncSession,
    business_id: UUID,
    table_id: UUID,
    *,
    exclude_sale_id: UUID | None = None,
) -> int:
    stmt = (
        select(func.count())
        .select_from(Sale)
        .where(
            Sale.business_id == business_id,
            Sale.table_id == table_id,
            Sale.deleted_at.is_(None),
            Sale.status.in_(ACTIVE_TAB_STATUSES),
        )
    )
    if exclude_sale_id is not None:
        stmt = stmt.where(Sale.id != exclude_sale_id)
    result = await db.execute(stmt)
    return int(result.scalar_one() or 0)


async def assert_no_active_tab_on_table(
    db: AsyncSession,
    business_id: UUID,
    table_id: UUID,
    *,
    exclude_sale_id: UUID | None = None,
) -> None:
    if await _count_active_tabs_on_table(
        db, business_id, table_id, exclude_sale_id=exclude_sale_id
    ) > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Table already has an active tab",
        )


async def _get_tab_sale_for_update(
    db: AsyncSession,
    sale_id: UUID,
    business_id: UUID,
) -> Sale:
    result = await db.execute(
        select(Sale)
        .where(
            Sale.id == sale_id,
            Sale.business_id == business_id,
            Sale.deleted_at.is_(None),
        )
        .options(selectinload(Sale.lines), selectinload(Sale.payments))
        .with_for_update()
    )
    sale = result.scalar_one_or_none()
    if sale is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sale not found",
        )
    if sale.sale_type != SaleTypeEnum.dine_in.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Sale is not a dine-in tab",
        )
    return sale


def _assert_tab_mutable(sale: Sale) -> None:
    if sale.status not in ACTIVE_TAB_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tab is not open (status is '{sale.status}')",
        )


def _tab_invoice_total(sale: Sale) -> Decimal:
    total = Decimal("0")
    for line in sale.lines:
        if line.deleted_at is None:
            total += line.qty * line.unit_price - line.discount_amount + line.tax_amount
    return _round2(total)


async def _prepare_tab_line_pricing(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID,
    line_data: TabSaleLineRequest,
    *,
    price_list_id: UUID | None,
    permission_keys: set[str],
) -> tuple[Decimal, Decimal, Decimal, str | None, list[dict[str, Any]]]:
    from app.services.sale_service import _enforce_sale_pricing_permissions

    modifier_snapshots = await validate_line_modifiers(
        db,
        business_id,
        line_data.product_id,
        line_data.modifier_ids,
    )
    modifier_total = sum(
        (item["price_delta"] for item in modifier_snapshots),
        Decimal("0"),
    )
    effective_unit_price = _round2(line_data.unit_price + modifier_total)

    await verify_product(db, line_data.product_id, business_id)
    if line_data.variation_id is not None:
        await verify_variation(
            db, line_data.variation_id, line_data.product_id, business_id
        )

    priced_line = TabSaleLineRequest(
        product_id=line_data.product_id,
        variation_id=line_data.variation_id,
        qty=line_data.qty,
        unit_price=line_data.unit_price,
        discount_pct=line_data.discount_pct,
        discount_amount=line_data.discount_amount,
        tax_rate=line_data.tax_rate,
        notes=line_data.notes,
        line_order=line_data.line_order,
    )
    await _enforce_sale_pricing_permissions(
        db,
        business_id,
        CreateSaleRequest(
            branch_id=branch_id,
            price_list_id=price_list_id,
            lines=[priced_line],
        ),
        permission_keys,
    )

    discount_pct, discount_amount = merge_line_discount_inputs(line_data, None)
    amounts = calculate_line_total(
        qty=line_data.qty,
        unit_price=effective_unit_price,
        discount_pct=discount_pct,
        discount_amount=discount_amount,
        tax_rate=line_data.tax_rate,
    )
    encoded_notes = _encode_sale_line_notes(line_data.notes, modifier_snapshots)
    return (
        effective_unit_price,
        amounts["effective_discount"],
        amounts["tax_amount"],
        encoded_notes,
        modifier_snapshots,
    )


async def open_tab(
    db: AsyncSession,
    business_id: UUID,
    data: OpenTabRequest,
    created_by: UUID,
) -> Sale:
    config = await _get_business_config(db, business_id)
    require_tab_feature_flags(config, "enable_restaurant", "enable_table_management")

    await verify_branch(db, data.branch_id, business_id)
    table = await get_dining_table_by_id(db, data.table_id, business_id)
    if table.branch_id != data.branch_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Table does not belong to the specified branch",
        )

    await assert_no_active_tab_on_table(db, business_id, data.table_id)

    if data.register_shift_id is not None:
        await verify_open_register_shift(
            db,
            data.register_shift_id,
            business_id,
            closed_detail="Cannot open tab — shift is not open",
        )
    if data.customer_id is not None:
        await get_customer_by_id(db, data.customer_id, business_id)

    now = _now()
    sale_number = await generate_document_number(
        db, business_id, "INV", Sale, Sale.sale_number
    )

    try:
        sale = Sale(
            business_id=business_id,
            branch_id=data.branch_id,
            customer_id=data.customer_id,
            price_list_id=data.price_list_id,
            table_id=data.table_id,
            sale_number=sale_number,
            sale_type=SaleTypeEnum.dine_in.value,
            status=SaleStatusEnum.held.value,
            sold_at=now,
            notes=data.notes,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(sale)
        await db.flush()
        if data.register_shift_id is not None:
            sale.register_shift_id = data.register_shift_id

        await transition_table_status(
            db,
            table,
            TableStatusEnum.occupied,
            updated_by=created_by,
            commit=False,
        )
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    from app.services.sale_service import get_sale_by_id

    return await get_sale_by_id(db, sale.id, business_id)


async def add_lines_to_tab(
    db: AsyncSession,
    business_id: UUID,
    sale_id: UUID,
    data: AddSaleLinesRequest,
    created_by: UUID,
    permission_keys: set[str],
) -> Sale:
    config = await _get_business_config(db, business_id)
    require_tab_feature_flags(config, "enable_restaurant")

    try:
        sale = await _get_tab_sale_for_update(db, sale_id, business_id)
        _assert_tab_mutable(sale)
        now = _now()
        next_line_order = max(
            (line.line_order for line in sale.lines if line.deleted_at is None),
            default=-1,
        ) + 1

        for line_data in data.lines:
            if line_data.line_order == 0:
                line_data = line_data.model_copy(
                    update={"line_order": next_line_order}
                )
                next_line_order += 1

            (
                effective_unit_price,
                effective_discount,
                tax_amount,
                encoded_notes,
                _modifiers,
            ) = await _prepare_tab_line_pricing(
                db,
                business_id,
                sale.branch_id,
                line_data,
                price_list_id=sale.price_list_id,
                permission_keys=permission_keys,
            )

            sale_line = SaleLine(
                business_id=business_id,
                sale_id=sale.id,
                product_id=line_data.product_id,
                variation_id=line_data.variation_id,
                qty=line_data.qty,
                unit_price=effective_unit_price,
                discount_pct=line_data.discount_pct,
                discount_amount=effective_discount,
                tax_rate=line_data.tax_rate,
                tax_amount=tax_amount,
                cost_per_unit=Decimal("0"),
                notes=encoded_notes,
                line_order=line_data.line_order,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            db.add(sale_line)

        sale.updated_by = created_by
        sale.updated_at = now
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    from app.services.sale_service import get_sale_by_id

    return await get_sale_by_id(db, sale_id, business_id)


async def _get_fired_sale_line_ids(
    db: AsyncSession,
    business_id: UUID,
    sale_id: UUID,
) -> set[UUID]:
    result = await db.execute(
        select(KotOrderLine.sale_line_id).where(
            KotOrderLine.business_id == business_id,
            KotOrderLine.sale_line_id.is_not(None),
            KotOrderLine.kot_order_id.in_(
                select(KotOrder.id).where(
                    KotOrder.business_id == business_id,
                    KotOrder.sale_id == sale_id,
                    KotOrder.deleted_at.is_(None),
                    KotOrder.status != KotStatusEnum.cancelled.value,
                )
            ),
        )
    )
    return {row[0] for row in result.all() if row[0] is not None}


async def fire_tab_to_kitchen(
    db: AsyncSession,
    business_id: UUID,
    sale_id: UUID,
    *,
    sale_line_ids: list[UUID] | None,
    notes: str | None,
    created_by: UUID,
) -> KotOrder:
    config = await _get_business_config(db, business_id)
    require_tab_feature_flags(config, "enable_restaurant", "enable_kot")

    try:
        sale = await _get_tab_sale_for_update(db, sale_id, business_id)
        _assert_tab_mutable(sale)

        fired_ids = await _get_fired_sale_line_ids(db, business_id, sale_id)
        unfired_lines = [
            line
            for line in sale.lines
            if line.deleted_at is None and line.id not in fired_ids
        ]
        if not unfired_lines:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No unfired lines to send to kitchen",
            )

        if sale_line_ids is not None:
            requested = set(sale_line_ids)
            unfired_lines = [line for line in unfired_lines if line.id in requested]
            if not unfired_lines:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="No matching unfired lines to send to kitchen",
                )

        now = _now()
        kot_number = await generate_document_number(
            db, business_id, "KOT", KotOrder, KotOrder.kot_number
        )
        kot_order = KotOrder(
            business_id=business_id,
            branch_id=sale.branch_id,
            sale_id=sale.id,
            table_id=sale.table_id,
            kot_number=kot_number,
            status=KotStatusEnum.pending.value,
            fired_at=now,
            notes=notes,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(kot_order)
        await db.flush()

        for line in unfired_lines:
            kitchen_notes, modifiers = decode_sale_line_notes(line.notes)
            kot_line_modifiers = [
                {
                    "modifier_id": str(item["modifier_id"]),
                    "name": item["name"],
                    "price_delta": str(item["price_delta"]),
                }
                for item in modifiers
            ]
            db.add(
                KotOrderLine(
                    business_id=business_id,
                    kot_order_id=kot_order.id,
                    sale_line_id=line.id,
                    product_id=line.product_id,
                    variation_id=line.variation_id,
                    qty=line.qty,
                    modifiers_json=kot_line_modifiers,
                    kitchen_notes=kitchen_notes,
                    status=KotStatusEnum.pending.value,
                    created_by=created_by,
                    created_at=now,
                    updated_at=now,
                )
            )

        sale.updated_by = created_by
        sale.updated_at = now
        await db.commit()
        await db.refresh(kot_order)
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    return kot_order


async def request_tab_bill(
    db: AsyncSession,
    business_id: UUID,
    sale_id: UUID,
    updated_by: UUID,
) -> Sale:
    config = await _get_business_config(db, business_id)
    require_tab_feature_flags(config, "enable_restaurant", "enable_table_management")

    try:
        sale = await _get_tab_sale_for_update(db, sale_id, business_id)
        _assert_tab_mutable(sale)
        if sale.table_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tab has no table assigned",
            )

        table = await get_dining_table_by_id(db, sale.table_id, business_id)
        await transition_table_status(
            db,
            table,
            TableStatusEnum.billing,
            updated_by=updated_by,
            commit=False,
        )

        sale.updated_by = updated_by
        sale.updated_at = _now()
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    from app.services.sale_service import get_sale_by_id

    return await get_sale_by_id(db, sale_id, business_id)


async def complete_tab(
    db: AsyncSession,
    business_id: UUID,
    sale_id: UUID,
    data: CompleteTabRequest,
    completed_by: UUID,
    permission_keys: set[str],
) -> Sale:
    config = await _get_business_config(db, business_id)
    require_tab_feature_flags(config, "enable_restaurant", "enable_table_management")

    allow_negative = await get_allow_negative_stock(db, business_id)
    now = _now()
    sold_at = data.sold_at or now

    try:
        sale = await _get_tab_sale_for_update(db, sale_id, business_id)
        _assert_tab_mutable(sale)

        active_lines = [line for line in sale.lines if line.deleted_at is None]
        if not active_lines:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tab has no lines to complete",
            )

        register_shift_id = data.register_shift_id or sale.register_shift_id
        if register_shift_id is not None:
            await verify_open_register_shift(
                db,
                register_shift_id,
                business_id,
                closed_detail="Cannot complete tab — shift is not open",
            )
            sale.register_shift_id = register_shift_id

        if not allow_negative:
            for line in active_lines:
                sufficient = await check_sufficient_stock(
                    db,
                    business_id,
                    sale.branch_id,
                    line.product_id,
                    line.variation_id,
                    line.qty,
                )
                if not sufficient:
                    product = await verify_product(
                        db, line.product_id, business_id
                    )
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Insufficient stock for {product.name}",
                    )

        total_amount = _tab_invoice_total(sale)
        total_paid = sum((p.amount for p in data.payments), Decimal("0"))
        if total_paid < total_amount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Payments do not cover tab total",
            )

        credit_payments = [
            p for p in data.payments if p.payment_method == PaymentMethodEnum.credit
        ]
        if credit_payments and sale.customer_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="customer_id is required for credit payments",
            )

        for line in active_lines:
            cost_per_unit, consumptions, short_qty = await allocate_sale_line_fifo(
                db,
                business_id,
                line.product_id,
                line.variation_id,
                line.qty,
            )
            line.cost_per_unit = cost_per_unit
            line.updated_at = now
            consumed_from_layers = line.qty - short_qty
            await create_sale_line_stock_movements(
                db,
                business_id=business_id,
                branch_id=sale.branch_id,
                product_id=line.product_id,
                variation_id=line.variation_id,
                sale_line_id=line.id,
                consumptions=consumptions,
                created_by=completed_by,
                movement_at=sold_at,
            )
            if short_qty > Decimal("0"):
                fallback_cost = next(
                    (
                        c.cost_per_unit
                        for c in reversed(consumptions)
                        if c.purchase_line_id is None
                    ),
                    Decimal("0"),
                )
                await maybe_notify_fifo_shortfall(
                    db,
                    business_id=business_id,
                    branch_id=sale.branch_id,
                    product_id=line.product_id,
                    variation_id=line.variation_id,
                    sale_line_id=line.id,
                    requested_qty=line.qty,
                    consumed_from_layers_qty=consumed_from_layers,
                    short_qty=short_qty,
                    fallback_cost_per_unit=fallback_cost,
                    created_by=completed_by,
                )

        for payment_data in data.payments:
            paid_at = payment_data.paid_at or now
            payment = SalePayment(
                business_id=business_id,
                sale_id=sale.id,
                payment_method=payment_data.payment_method.value,
                amount=payment_data.amount,
                status="completed",
                reference_no=payment_data.reference_no,
                paid_at=paid_at,
                created_by=completed_by,
                created_at=now,
                updated_at=now,
            )
            db.add(payment)
            if register_shift_id is not None:
                db.add(
                    RegisterTransaction(
                        business_id=business_id,
                        register_shift_id=register_shift_id,
                        tx_type=RegisterTxTypeEnum.sale.value,
                        payment_method=payment.payment_method,
                        amount=payment.amount,
                        reference_type=ReferenceTypeEnum.sale.value,
                        reference_id=sale.id,
                        transacted_at=paid_at,
                        created_by=completed_by,
                        created_at=now,
                        updated_at=now,
                    )
                )

        if sale.customer_id is not None and credit_payments:
            credit_amount = sum((p.amount for p in credit_payments), Decimal("0"))
            await create_ledger_entry(
                db=db,
                business_id=business_id,
                customer_id=sale.customer_id,
                entry_type=LedgerEntryTypeEnum.sale,
                amount=-credit_amount,
                created_by=completed_by,
                reference_type=ReferenceTypeEnum.sale,
                reference_id=sale.id,
            )

        sale.status = determine_sale_status(total_amount, total_paid).value
        sale.sold_at = sold_at
        sale.updated_by = completed_by
        sale.updated_at = now

        if sale.table_id is not None:
            table = await get_dining_table_by_id(db, sale.table_id, business_id)
            current = TableStatusEnum(table.status)
            if current == TableStatusEnum.billing:
                await transition_table_status(
                    db,
                    table,
                    TableStatusEnum.cleaning,
                    updated_by=completed_by,
                    commit=False,
                )
            elif current == TableStatusEnum.occupied:
                await transition_table_status(
                    db,
                    table,
                    TableStatusEnum.cleaning,
                    updated_by=completed_by,
                    force=True,
                    commit=False,
                )

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    from app.services.sale_service import get_sale_by_id

    return await get_sale_by_id(db, sale_id, business_id)


async def maybe_release_dining_table(
    db: AsyncSession,
    sale: Sale,
    *,
    updated_by: UUID,
) -> None:
    if sale.table_id is None:
        return
    if sale.sale_type != SaleTypeEnum.dine_in.value:
        return

    remaining = await _count_active_tabs_on_table(
        db, sale.business_id, sale.table_id
    )
    if remaining > 0:
        return

    table = await get_dining_table_by_id(db, sale.table_id, sale.business_id)
    await transition_table_status(
        db,
        table,
        TableStatusEnum.available,
        updated_by=updated_by,
        force=True,
        commit=False,
    )


async def cancel_kot_orders_for_sale(
    db: AsyncSession,
    business_id: UUID,
    sale_id: UUID,
) -> None:
    result = await db.execute(
        select(KotOrder).where(
            KotOrder.business_id == business_id,
            KotOrder.sale_id == sale_id,
            KotOrder.deleted_at.is_(None),
            KotOrder.status != KotStatusEnum.cancelled.value,
        )
    )
    now = _now()
    for kot_order in result.scalars().all():
        kot_order.status = KotStatusEnum.cancelled.value
        kot_order.updated_at = now


async def on_held_tab_cancelled(
    db: AsyncSession,
    sale: Sale,
    *,
    cancelled_by: UUID,
) -> None:
    if sale.sale_type != SaleTypeEnum.dine_in.value:
        return
    if sale.status != SaleStatusEnum.cancelled.value:
        return

    config = await _get_business_config(db, sale.business_id)
    if not get_feature_flag(config.config_json, "enable_restaurant"):
        return

    await cancel_kot_orders_for_sale(db, sale.business_id, sale.id)
    if get_feature_flag(config.config_json, "enable_table_management"):
        await maybe_release_dining_table(db, sale, updated_by=cancelled_by)
