"""POST /products/import: CSV/rows -> products, with a per-row validation report.

Idempotent: a row is matched to an existing product by ``sku`` first, then ``barcode``.
Matched rows update name/category/price (and add a missing barcode); re-importing the same
file reports every row ``unchanged``. Opening stock is applied only to products that have no
stock history yet, as one ``opening_balance`` adjustment, so a retry never double-counts.

``cost`` has no column on the product (costs live in FIFO layers), so it is only used as the
cost of the opening-stock layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.business import Branch
from app.models.enums import AdjustmentReasonEnum
from app.models.inventory import StockMovement
from app.models.product import (
    Barcode,
    Category,
    PriceListItem,
    Product,
    Unit,
)
from app.schemas.inventory import CreateAdjustmentLineRequest, CreateStockAdjustmentRequest
from app.services.adjustment_service import create_stock_adjustment
from app.services.import_common import (
    ImportRequest,
    RowResult,
    build_report,
    parse_decimal,
    parse_table,
    reject_with_report,
)
from app.services.price_service import ensure_default_price_list
from app.services.product_service import _create_variations

_CENT = Decimal("0.01")

PRODUCT_IMPORT_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("product_name", "item_name", "title"),
    "sku": ("item_code", "product_code", "code"),
    "barcode": ("bar_code", "ean", "upc"),
    "category": ("category_name", "group"),
    "unit": ("uom", "unit_name", "unit_symbol", "base_unit"),
    "cost": ("cost_price", "purchase_price", "buying_price"),
    "price": ("selling_price", "sale_price", "unit_price", "retail_price"),
    "opening_stock": ("stock", "opening_balance", "opening_qty", "qty", "quantity"),
}


class ProductImportRequest(ImportRequest):
    # Branch that receives opening stock. Optional when the business has one branch.
    branch_id: UUID | None = None


@dataclass
class _Plan:
    result: RowResult
    product_id: UUID | None = None  # None = create
    name: str | None = None
    sku: str | None = None
    barcode: str | None = None
    add_barcode: bool = False
    category_key: str | None = None  # lower-cased name
    category_name: str | None = None
    unit_id: UUID | None = None
    price: Decimal | None = None
    cost: Decimal | None = None
    opening: Decimal | None = None
    changed_name: bool = False
    changed_category: bool = False
    changed_price: bool = False


def _lc(value: str | None) -> str:
    return (value or "").strip().lower()


async def import_products(
    db: AsyncSession,
    business_id: UUID,
    body: ProductImportRequest,
    user_id: UUID,
    branch_id: UUID | None,
    can_adjust_stock: bool = True,
) -> dict[str, Any]:
    rows, unknown = parse_table(body.csv, body.rows, PRODUCT_IMPORT_ALIASES)

    # ── reference data ───────────────────────────────────────────────────────
    units: dict[str, UUID] = {}
    for unit in (
        await db.execute(
            select(Unit).where(Unit.business_id == business_id, Unit.deleted_at.is_(None))
        )
    ).scalars():
        units.setdefault(_lc(unit.name), unit.id)
        units.setdefault(_lc(unit.symbol), unit.id)
    categories: dict[str, tuple[UUID, str]] = {
        _lc(c.name): (c.id, c.name)
        for c in (
            await db.execute(
                select(Category).where(
                    Category.business_id == business_id, Category.deleted_at.is_(None)
                )
            )
        ).scalars()
    }

    skus = {r.values["sku"] for r in rows if r.values.get("sku")}
    codes = {r.values["barcode"] for r in rows if r.values.get("barcode")}
    # sku/barcode uniqueness in the database also covers soft-deleted rows, so look at all.
    by_sku: dict[str, Product] = {}
    if skus:
        by_sku = {
            p.sku: p
            for p in (
                await db.execute(
                    select(Product).where(Product.business_id == business_id, Product.sku.in_(skus))
                )
            ).scalars()
        }
    by_code: dict[str, Barcode] = {}
    if codes:
        by_code = {
            b.barcode: b
            for b in (
                await db.execute(
                    select(Barcode).where(
                        Barcode.business_id == business_id, Barcode.barcode.in_(codes)
                    )
                )
            ).scalars()
        }
    matched_ids = {p.id for p in by_sku.values() if p.deleted_at is None} | {
        b.product_id for b in by_code.values() if b.deleted_at is None
    }
    products_by_id: dict[UUID, Product] = {}
    current_price: dict[UUID, Decimal] = {}
    with_history: set[UUID] = set()
    product_barcodes: dict[UUID, set[str]] = {}
    default_list_id = await ensure_default_price_list(db, business_id)
    if matched_ids:
        products_by_id = {
            p.id: p
            for p in (
                await db.execute(
                    select(Product).where(
                        Product.business_id == business_id,
                        Product.id.in_(matched_ids),
                        Product.deleted_at.is_(None),
                    )
                )
            ).scalars()
        }
        current_price = {
            row.product_id: row.unit_price
            for row in (
                await db.execute(
                    select(PriceListItem.product_id, PriceListItem.unit_price).where(
                        PriceListItem.business_id == business_id,
                        PriceListItem.price_list_id == default_list_id,
                        PriceListItem.product_id.in_(matched_ids),
                        PriceListItem.variation_id.is_(None),
                        PriceListItem.min_qty == 1,
                        PriceListItem.deleted_at.is_(None),
                    )
                )
            ).all()
        }
        with_history = set(
            (
                await db.execute(
                    select(StockMovement.product_id)
                    .where(
                        StockMovement.business_id == business_id,
                        StockMovement.product_id.in_(matched_ids),
                        StockMovement.deleted_at.is_(None),
                    )
                    .distinct()
                )
            ).scalars()
        )
        for b in (
            await db.execute(
                select(Barcode.product_id, Barcode.barcode).where(
                    Barcode.business_id == business_id,
                    Barcode.product_id.in_(matched_ids),
                    Barcode.deleted_at.is_(None),
                )
            )
        ).all():
            product_barcodes.setdefault(b.product_id, set()).add(b.barcode)

    # ── per-row validation ───────────────────────────────────────────────────
    plans: list[_Plan] = []
    seen_sku: dict[str, int] = {}
    seen_code: dict[str, int] = {}
    for row in rows:
        v = row.values
        sku = v.get("sku") or None
        code = v.get("barcode") or None
        name = v.get("name") or None
        result = RowResult(row.number, identity={"sku": sku, "barcode": code, "name": name})
        plan = _Plan(result=result, name=name, sku=sku, barcode=code)
        plans.append(plan)

        if not sku and not code:
            result.error("sku or barcode is required (rows are matched on them)")
            continue
        if sku and len(sku) > 100:
            result.error("sku is longer than 100 characters")
        if code and not 4 <= len(code) <= 100:
            result.error("barcode must be 4-100 characters")
        if name and not 2 <= len(name) <= 255:
            result.error("name must be 2-255 characters")
        for value, seen, label in ((sku, seen_sku, "sku"), (code, seen_code, "barcode")):
            if value and value in seen:
                result.error(f"duplicate {label} '{value}' (first used in row {seen[value]})")
            elif value:
                seen[value] = row.number

        sku_hit = by_sku.get(sku) if sku else None
        code_hit = by_code.get(code) if code else None
        if sku_hit is not None and sku_hit.deleted_at is not None:
            result.error(f"sku '{sku}' belongs to a deleted product")
        if code_hit is not None and code_hit.deleted_at is not None:
            result.error(f"barcode '{code}' belongs to a deleted product")
        if (
            sku_hit is not None
            and code_hit is not None
            and not result.errors
            and sku_hit.id != code_hit.product_id
        ):
            result.error("sku and barcode belong to different products")

        for field, label in (("price", "price"), ("cost", "cost"), ("opening_stock", "opening_stock")):
            if v.get(field):
                try:
                    value = parse_decimal(v[field], label)
                except ValueError as exc:
                    result.error(str(exc))
                    continue
                if field == "price":
                    plan.price = value.quantize(_CENT, rounding=ROUND_HALF_UP)
                elif field == "cost":
                    plan.cost = value.quantize(_CENT, rounding=ROUND_HALF_UP)
                else:
                    plan.opening = value
        if result.errors:
            continue

        target_id = (sku_hit.id if sku_hit is not None else None) or (
            code_hit.product_id if code_hit is not None else None
        )
        product = products_by_id.get(target_id) if target_id else None
        if v.get("category"):
            plan.category_key = _lc(v["category"])
            plan.category_name = v["category"]
        unit_text = v.get("unit")
        unit_id = units.get(_lc(unit_text)) if unit_text else None

        if product is None:
            if not name:
                result.error("name is required for a new product")
            if not unit_text:
                result.error("unit is required for a new product")
            elif unit_id is None:
                result.error(f"unknown unit '{unit_text}'")
            if result.errors:
                continue
            plan.unit_id = unit_id
            result.action = "create"
            if plan.category_key and plan.category_key not in categories:
                result.warnings.append(f"category '{plan.category_name}' will be created")
            if plan.cost is not None and not plan.opening:
                result.warnings.append("cost ignored: it is only used for opening stock")
            if plan.opening and plan.opening > 0:
                if not can_adjust_stock:
                    result.error("opening stock needs the inventory.adjust permission")
                elif branch_id is None:
                    result.error("branch_id is required to import opening stock")
            result.details["changes"] = ["new product"]
            continue

        # existing product
        plan.product_id = product.id
        changes: list[str] = []
        if name and name != product.name:
            plan.changed_name = True
            changes.append("name")
        if plan.category_key is not None:
            current = categories.get(plan.category_key)
            if current is None or current[0] != product.category_id:
                plan.changed_category = True
                changes.append("category")
                if current is None:
                    result.warnings.append(f"category '{plan.category_name}' will be created")
        if plan.price is not None and current_price.get(product.id) != plan.price:
            plan.changed_price = True
            changes.append("price")
        if code and code not in product_barcodes.get(product.id, set()):
            plan.add_barcode = True
            changes.append("barcode")
        if unit_text and unit_id != product.base_unit_id:
            result.warnings.append("unit differs from the existing product and was ignored")
        if plan.opening and plan.opening > 0:
            if product.id in with_history:
                result.warnings.append("opening stock skipped: the product already has stock history")
                plan.opening = None
            elif not can_adjust_stock:
                result.error("opening stock needs the inventory.adjust permission")
            elif branch_id is None:
                result.error("branch_id is required to import opening stock")
            else:
                changes.append("opening stock")
        result.details["changes"] = changes
        result.action = "update" if changes else "unchanged"

    results = [p.result for p in plans]
    errors = any(r.action == "error" for r in results)
    if body.dry_run:
        return build_report(results, unknown, dry_run=True)
    if errors and body.on_error == "abort":
        raise reject_with_report(build_report(results, unknown, dry_run=False))

    work = [p for p in plans if p.result.action in ("create", "update")]
    if not work:
        return build_report(results, unknown, dry_run=False, applied=0)

    # ── apply ────────────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    new_categories: dict[str, Category] = {}
    for plan in work:
        key = plan.category_key
        if key and key not in categories and key not in new_categories:
            category = Category(
                business_id=business_id,
                name=plan.category_name,
                sort_order=0,
                is_active=True,
                created_by=user_id,
                created_at=now,
                updated_at=now,
            )
            db.add(category)
            new_categories[key] = category
    if new_categories:
        await db.flush()

    def category_id_for(key: str | None) -> UUID | None:
        if not key:
            return None
        if key in categories:
            return categories[key][0]
        return new_categories[key].id

    opening_lines: list[CreateAdjustmentLineRequest] = []
    price_entries: list[tuple[UUID, Decimal]] = []
    try:
        for plan in work:
            if plan.product_id is None:
                product = Product(
                    business_id=business_id,
                    category_id=category_id_for(plan.category_key),
                    base_unit_id=plan.unit_id,
                    name=plan.name,
                    sku=plan.sku,
                    product_type="standard",
                    tracking_type="none",
                    is_sellable=True,
                    is_purchasable=True,
                    is_active=True,
                    created_by=user_id,
                    created_at=now,
                    updated_at=now,
                )
                db.add(product)
                await db.flush()
                await _create_variations(db, product, [], user_id, now)
                if plan.barcode:
                    db.add(
                        Barcode(
                            business_id=business_id,
                            product_id=product.id,
                            barcode=plan.barcode,
                            barcode_type="EAN13",
                            is_primary=True,
                            created_by=user_id,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                pid = product.id
            else:
                product = products_by_id[plan.product_id]
                if plan.changed_name:
                    product.name = plan.name
                if plan.changed_category:
                    product.category_id = category_id_for(plan.category_key)
                if plan.changed_name or plan.changed_category:
                    product.updated_by = user_id
                    product.updated_at = now
                if plan.add_barcode and plan.barcode:
                    has_primary = bool(product_barcodes.get(product.id))
                    db.add(
                        Barcode(
                            business_id=business_id,
                            product_id=product.id,
                            barcode=plan.barcode,
                            barcode_type="EAN13",
                            is_primary=not has_primary,
                            created_by=user_id,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                pid = product.id
            if plan.price is not None and (plan.product_id is None or plan.changed_price):
                price_entries.append((pid, plan.price))
            if plan.opening and plan.opening > 0:
                opening_lines.append(
                    CreateAdjustmentLineRequest(
                        product_id=pid, qty_delta=plan.opening, cost_per_unit=plan.cost
                    )
                )

        if price_entries:
            existing = {
                i.product_id: i
                for i in (
                    await db.execute(
                        select(PriceListItem).where(
                            PriceListItem.business_id == business_id,
                            PriceListItem.price_list_id == default_list_id,
                            PriceListItem.variation_id.is_(None),
                            PriceListItem.min_qty == 1,
                            PriceListItem.product_id.in_([p for p, _ in price_entries]),
                        )
                    )
                ).scalars()
            }
            for pid, price in price_entries:
                item = existing.get(pid)
                if item is None:
                    db.add(
                        PriceListItem(
                            business_id=business_id,
                            price_list_id=default_list_id,
                            product_id=pid,
                            unit_price=price,
                            min_qty=Decimal("1"),
                            created_by=user_id,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                else:  # includes a soft-deleted twin holding the unique key
                    item.unit_price = price
                    item.deleted_at = None
                    item.deleted_by = None
                    item.updated_by = user_id
                    item.updated_at = now
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        raise

    if opening_lines:
        try:
            await create_stock_adjustment(
                db,
                business_id,
                CreateStockAdjustmentRequest(
                    branch_id=branch_id,
                    reason=AdjustmentReasonEnum.opening_balance,
                    notes="Product import: opening stock",
                    lines=opening_lines,
                ),
                user_id,
            )
        except HTTPException as exc:
            raise HTTPException(
                exc.status_code,
                f"Products were imported but opening stock failed: {exc.detail}. "
                "Run the import again to apply the opening stock.",
            ) from exc

    return build_report(results, unknown, dry_run=False, applied=len(work))


async def resolve_import_branch(
    db: AsyncSession, business_id: UUID, branch_id: UUID | None
) -> UUID | None:
    """The branch for opening stock: the given one, else the only active branch, else None."""
    if branch_id is not None:
        return branch_id
    ids = list(
        (
            await db.execute(
                select(Branch.id).where(
                    Branch.business_id == business_id, Branch.deleted_at.is_(None)
                ).limit(2)
            )
        ).scalars()
    )
    return ids[0] if len(ids) == 1 else None
