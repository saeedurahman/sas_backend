"""Price-list management: update/delete/copy, item paging, bulk upsert, percent adjust, CSV import.

Every write here goes through ORM/UPDATE statements on synced tables, so the 019 change_seq
triggers stamp each touched row and devices receive the changes through the sync feed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.product import Barcode, PriceList, PriceListItem, Product, ProductVariation
from app.models.sales import Customer
from app.services.audit_helper import log_audit
from app.services.import_common import (
    ImportRequest,
    RowResult,
    build_report,
    parse_decimal,
    parse_table,
    reject_with_report,
)
from app.services.price_service import _get_price_list_by_id, with_counts

_CENT = Decimal("0.01")


def _name_taken() -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, "A price list with this name already exists")


def _audit(db, business_id, user_id, action, table, record_id, old=None, new=None):
    return log_audit(
        db,
        business_id=business_id,
        user_id=user_id,
        branch_id=None,  # price lists are business-wide
        action=action,
        table_name=table,
        record_id=record_id,
        old_values=old,
        new_values=new,
    )


async def get_price_list(db: AsyncSession, business_id: UUID, list_id: UUID) -> PriceList:
    price_list = await _get_price_list_by_id(db, list_id, business_id)
    return (await with_counts(db, business_id, [price_list]))[0]


# ── list-level ───────────────────────────────────────────────────────────────


async def update_price_list(
    db: AsyncSession, business_id: UUID, list_id: UUID, data, user_id: UUID
) -> PriceList:
    price_list = await _get_price_list_by_id(db, list_id, business_id)
    sent = data.model_fields_set
    now = datetime.now(timezone.utc)

    makes_default = data.is_default is True and not price_list.is_default
    if data.is_default is False and price_list.is_default:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A business always has one default list: make another list the default instead",
        )
    will_be_default = price_list.is_default or makes_default
    if will_be_default and data.is_active is False:
        raise HTTPException(status.HTTP_409_CONFLICT, "The default price list must stay active")
    new_from = data.valid_from if "valid_from" in sent else price_list.valid_from
    new_to = data.valid_to if "valid_to" in sent else price_list.valid_to
    if will_be_default and (new_from is not None or new_to is not None):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "The default price list cannot have a validity window"
        )
    if new_from is not None and new_to is not None and new_to <= new_from:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "valid_to must be after valid_from"
        )

    old = {
        "name": price_list.name,
        "list_type": price_list.list_type,
        "is_default": price_list.is_default,
        "is_active": price_list.is_active,
    }
    try:
        if makes_default:
            # The partial unique index is immediate: demote the old default first.
            await db.execute(
                update(PriceList)
                .where(
                    PriceList.business_id == business_id,
                    PriceList.deleted_at.is_(None),
                    PriceList.is_default.is_(True),
                )
                .values(is_default=False, updated_by=user_id, updated_at=now)
            )
            price_list.is_default = True
            price_list.is_active = True
        if data.name is not None:
            price_list.name = data.name
        if data.list_type is not None:
            price_list.list_type = data.list_type.value
        if data.is_active is not None and not makes_default:
            price_list.is_active = data.is_active
        if "valid_from" in sent:
            price_list.valid_from = data.valid_from
        if "valid_to" in sent:
            price_list.valid_to = data.valid_to
        price_list.updated_by = user_id
        price_list.updated_at = now
        await _audit(
            db, business_id, user_id, "update", "price_lists", price_list.id, old,
            {k: str(v) for k, v in data.model_dump(exclude_unset=True).items()},
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _name_taken() from None
    return await get_price_list(db, business_id, list_id)


async def delete_price_list(
    db: AsyncSession, business_id: UUID, list_id: UUID, user_id: UUID
) -> dict[str, Any]:
    price_list = await _get_price_list_by_id(db, list_id, business_id)
    if price_list.is_default:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The default price list cannot be deleted: make another list the default first",
        )
    now = datetime.now(timezone.utc)
    unassigned = (
        await db.execute(
            update(Customer)
            .where(Customer.business_id == business_id, Customer.price_list_id == list_id)
            .values(price_list_id=None, updated_by=user_id, updated_at=now)
        )
    ).rowcount or 0
    await db.execute(
        update(PriceListItem)
        .where(
            PriceListItem.business_id == business_id,
            PriceListItem.price_list_id == list_id,
            PriceListItem.deleted_at.is_(None),
        )
        .values(deleted_at=now, deleted_by=user_id, updated_at=now)
    )
    # (business_id, name) stays unique across deleted rows; free the name for reuse.
    price_list.name = f"{price_list.name[:80]} [deleted {str(list_id)[:8]}]"
    price_list.is_active = False
    price_list.deleted_at = now
    price_list.deleted_by = user_id
    price_list.updated_at = now
    await _audit(
        db, business_id, user_id, "delete", "price_lists", list_id,
        None, {"customers_unassigned": unassigned},
    )
    await db.commit()
    return {"deleted": True, "customers_unassigned": unassigned}


async def copy_price_list(
    db: AsyncSession, business_id: UUID, list_id: UUID, data, user_id: UUID
) -> PriceList:
    source = await _get_price_list_by_id(db, list_id, business_id)
    now = datetime.now(timezone.utc)
    new_list = PriceList(
        business_id=business_id,
        name=data.name,
        list_type=(data.list_type.value if data.list_type else source.list_type),
        is_default=False,
        is_active=True,
        valid_from=data.valid_from,
        valid_to=data.valid_to,
        created_by=user_id,
        created_at=now,
        updated_at=now,
    )
    try:
        db.add(new_list)
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise _name_taken() from None

    items = (
        await db.execute(
            select(PriceListItem).where(
                PriceListItem.business_id == business_id,
                PriceListItem.price_list_id == list_id,
                PriceListItem.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    for item in items:
        price = item.unit_price if data.percent is None else _adjusted(item.unit_price, data.percent, None)
        db.add(
            PriceListItem(
                business_id=business_id,
                price_list_id=new_list.id,
                product_id=item.product_id,
                variation_id=item.variation_id,
                unit_price=price,
                min_qty=item.min_qty,
                created_by=user_id,
                created_at=now,
                updated_at=now,
            )
        )
    await _audit(
        db, business_id, user_id, "create", "price_lists", new_list.id,
        None, {"copied_from": str(list_id), "items": len(items)},
    )
    await db.commit()
    return await get_price_list(db, business_id, new_list.id)


# ── items ────────────────────────────────────────────────────────────────────


async def list_items(
    db: AsyncSession,
    business_id: UUID,
    list_id: UUID,
    search: str | None,
    skip: int,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    await _get_price_list_by_id(db, list_id, business_id)
    conditions = [
        PriceListItem.business_id == business_id,
        PriceListItem.price_list_id == list_id,
        PriceListItem.deleted_at.is_(None),
    ]
    if search and search.strip():
        escaped = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        conditions.append(
            or_(
                Product.name.ilike(like),
                Product.sku.ilike(like),
                ProductVariation.name.ilike(like),
                ProductVariation.sku.ilike(like),
                exists().where(
                    Barcode.product_id == Product.id,
                    Barcode.deleted_at.is_(None),
                    Barcode.barcode.ilike(like),
                ),
            )
        )
    base = (
        select(
            PriceListItem,
            Product.name.label("product_name"),
            Product.sku.label("sku"),
            ProductVariation.name.label("variation_name"),
        )
        .join(Product, Product.id == PriceListItem.product_id)
        .outerjoin(ProductVariation, ProductVariation.id == PriceListItem.variation_id)
        .where(*conditions)
    )
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await db.execute(
            base.order_by(Product.name, PriceListItem.min_qty, PriceListItem.id)
            .offset(skip)
            .limit(limit)
        )
    ).all()
    return [
        {
            "id": row.PriceListItem.id,
            "price_list_id": row.PriceListItem.price_list_id,
            "product_id": row.PriceListItem.product_id,
            "variation_id": row.PriceListItem.variation_id,
            "product_name": row.product_name,
            "variation_name": row.variation_name,
            "sku": row.sku,
            "unit_price": row.PriceListItem.unit_price,
            "min_qty": row.PriceListItem.min_qty,
            "updated_at": row.PriceListItem.updated_at,
        }
        for row in rows
    ], total


async def _upsert_entries(
    db: AsyncSession,
    business_id: UUID,
    list_id: UUID,
    entries: list[tuple[UUID, UUID | None, Decimal, Decimal]],
    user_id: UUID,
) -> tuple[int, int, int]:
    """Insert/update/revive ``(product, variation, price, min_qty)`` entries. No commit."""
    product_ids = {e[0] for e in entries}
    existing_rows = (
        await db.execute(
            select(PriceListItem).where(
                PriceListItem.business_id == business_id,
                PriceListItem.price_list_id == list_id,
                PriceListItem.product_id.in_(product_ids),
            )
        )
    ).scalars().all()
    existing = {(i.product_id, i.variation_id, i.min_qty): i for i in existing_rows}

    now = datetime.now(timezone.utc)
    created = updated = unchanged = 0
    for product_id, variation_id, raw_price, min_qty in entries:
        price = raw_price.quantize(_CENT, rounding=ROUND_HALF_UP)
        item = existing.get((product_id, variation_id, min_qty))
        if item is None:
            db.add(
                PriceListItem(
                    business_id=business_id,
                    price_list_id=list_id,
                    product_id=product_id,
                    variation_id=variation_id,
                    unit_price=price,
                    min_qty=min_qty,
                    created_by=user_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            created += 1
        elif item.deleted_at is not None:
            # A soft-deleted row still owns the unique key: bring it back.
            item.deleted_at = None
            item.deleted_by = None
            item.unit_price = price
            item.updated_by = user_id
            item.updated_at = now
            created += 1
        elif item.unit_price != price:
            item.unit_price = price
            item.updated_by = user_id
            item.updated_at = now
            updated += 1
        else:
            unchanged += 1
    return created, updated, unchanged


async def bulk_upsert_items(
    db: AsyncSession, business_id: UUID, list_id: UUID, data, user_id: UUID
) -> dict[str, int]:
    await _get_price_list_by_id(db, list_id, business_id)
    if len(data.items) > settings.price_bulk_max_items:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"At most {settings.price_bulk_max_items} items per request",
        )
    seen: set[tuple] = set()
    for entry in data.items:
        key = (entry.product_id, entry.variation_id, entry.min_qty)
        if key in seen:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Duplicate entry for product {entry.product_id} (min_qty {entry.min_qty})",
            )
        seen.add(key)

    product_ids = {e.product_id for e in data.items}
    found = set(
        (
            await db.execute(
                select(Product.id).where(
                    Product.business_id == business_id,
                    Product.deleted_at.is_(None),
                    Product.id.in_(product_ids),
                )
            )
        ).scalars()
    )
    missing = product_ids - found
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown product(s): {sorted(map(str, missing))[:5]}",
        )
    pairs = {(e.product_id, e.variation_id) for e in data.items if e.variation_id is not None}
    if pairs:
        valid = {
            (row.product_id, row.id)
            for row in (
                await db.execute(
                    select(ProductVariation.product_id, ProductVariation.id).where(
                        ProductVariation.business_id == business_id,
                        ProductVariation.deleted_at.is_(None),
                        ProductVariation.id.in_({v for _, v in pairs}),
                    )
                )
            ).all()
        }
        bad = [v for p, v in pairs if (p, v) not in valid]
        if bad:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Variation(s) not found for the given product: {sorted(map(str, bad))[:5]}",
            )

    created, updated, unchanged = await _upsert_entries(
        db,
        business_id,
        list_id,
        [(e.product_id, e.variation_id, e.price, e.min_qty) for e in data.items],
        user_id,
    )
    await _audit(
        db, business_id, user_id, "update", "price_list_items", list_id, None,
        {"bulk_upsert": {"created": created, "updated": updated, "unchanged": unchanged}},
    )
    await db.commit()
    return {"created": created, "updated": updated, "unchanged": unchanged, "total": len(data.items)}


async def delete_item(
    db: AsyncSession, business_id: UUID, list_id: UUID, item_id: UUID, user_id: UUID
) -> None:
    await _get_price_list_by_id(db, list_id, business_id)
    item = (
        await db.execute(
            select(PriceListItem).where(
                PriceListItem.id == item_id,
                PriceListItem.price_list_id == list_id,
                PriceListItem.business_id == business_id,
                PriceListItem.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Price list item not found")
    now = datetime.now(timezone.utc)
    item.deleted_at = now
    item.deleted_by = user_id
    item.updated_at = now
    await _audit(
        db, business_id, user_id, "delete", "price_list_items", item_id,
        {"unit_price": str(item.unit_price)}, None,
    )
    await db.commit()


# ── percent adjust ───────────────────────────────────────────────────────────


def _adjusted(price: Decimal, percent: Decimal, round_to: Decimal | None) -> Decimal:
    value = price * (Decimal("100") + percent) / Decimal("100")
    if round_to is not None:
        value = (value / round_to).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * round_to
    return max(value.quantize(_CENT, rounding=ROUND_HALF_UP), Decimal("0"))


async def adjust_prices(
    db: AsyncSession, business_id: UUID, list_id: UUID, data, user_id: UUID
) -> dict[str, Any]:
    await _get_price_list_by_id(db, list_id, business_id)
    stmt = select(PriceListItem).where(
        PriceListItem.business_id == business_id,
        PriceListItem.price_list_id == list_id,
        PriceListItem.deleted_at.is_(None),
    )
    if data.category_id is not None:
        stmt = stmt.where(
            PriceListItem.product_id.in_(
                select(Product.id).where(
                    Product.business_id == business_id, Product.category_id == data.category_id
                )
            )
        )
    items = (await db.execute(stmt)).scalars().all()
    now = datetime.now(timezone.utc)
    updated = unchanged = 0
    sample: list[dict[str, Any]] = []
    for item in items:
        new_price = _adjusted(item.unit_price, data.percent, data.round_to)
        if new_price == item.unit_price:
            unchanged += 1
            continue
        updated += 1
        if len(sample) < 5:
            sample.append(
                {
                    "item_id": item.id,
                    "product_id": item.product_id,
                    "old_price": item.unit_price,
                    "new_price": new_price,
                }
            )
        if not data.dry_run:
            item.unit_price = new_price
            item.updated_by = user_id
            item.updated_at = now
    if data.dry_run:
        await db.rollback()
    else:
        if updated:
            await _audit(
                db, business_id, user_id, "update", "price_list_items", list_id, None,
                {"percent_adjust": str(data.percent), "updated": updated},
            )
        await db.commit()
    return {"dry_run": data.dry_run, "updated": updated, "unchanged": unchanged, "sample": sample}


# ── CSV import ───────────────────────────────────────────────────────────────

PRICE_IMPORT_ALIASES: dict[str, tuple[str, ...]] = {
    "sku": ("product_sku", "item_code", "code"),
    "barcode": ("bar_code", "ean", "upc"),
    "price": ("unit_price", "new_price", "selling_price", "sale_price"),
    "min_qty": ("min_quantity", "minimum_qty", "qty_from"),
}


async def import_prices(
    db: AsyncSession, business_id: UUID, list_id: UUID, body: ImportRequest, user_id: UUID
) -> dict[str, Any]:
    """Dry-run (default) validates and reports; apply upserts the valid rows.

    Rows identify a product (or variation) by ``sku`` or ``barcode``. Re-importing the same
    file is a no-op: rows already at the file's price report ``unchanged``.
    """
    await _get_price_list_by_id(db, list_id, business_id)
    rows, unknown = parse_table(body.csv, body.rows, PRICE_IMPORT_ALIASES)

    skus = {r.values["sku"] for r in rows if r.values.get("sku")}
    barcodes = {r.values["barcode"] for r in rows if r.values.get("barcode")}
    by_sku: dict[str, tuple[UUID, UUID | None, str]] = {}
    if skus:
        for p in (
            await db.execute(
                select(Product.id, Product.sku, Product.name).where(
                    Product.business_id == business_id,
                    Product.deleted_at.is_(None),
                    Product.sku.in_(skus),
                )
            )
        ).all():
            by_sku[p.sku] = (p.id, None, p.name)
        for v in (
            await db.execute(
                select(ProductVariation.product_id, ProductVariation.id, ProductVariation.sku, Product.name)
                .join(Product, Product.id == ProductVariation.product_id)
                .where(
                    ProductVariation.business_id == business_id,
                    ProductVariation.deleted_at.is_(None),
                    Product.deleted_at.is_(None),
                    ProductVariation.sku.in_(skus),
                )
            )
        ).all():
            by_sku.setdefault(v.sku, (v.product_id, v.id, v.name))
    by_barcode: dict[str, tuple[UUID, UUID | None, str]] = {}
    if barcodes:
        for b in (
            await db.execute(
                select(Barcode.barcode, Barcode.product_id, Barcode.variation_id, Product.name)
                .join(Product, Product.id == Barcode.product_id)
                .where(
                    Barcode.business_id == business_id,
                    Barcode.deleted_at.is_(None),
                    Product.deleted_at.is_(None),
                    Barcode.barcode.in_(barcodes),
                )
            )
        ).all():
            by_barcode[b.barcode] = (b.product_id, b.variation_id, b.name)

    existing_rows = (
        await db.execute(
            select(PriceListItem).where(
                PriceListItem.business_id == business_id,
                PriceListItem.price_list_id == list_id,
                PriceListItem.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    current = {(i.product_id, i.variation_id, i.min_qty): i.unit_price for i in existing_rows}

    results: list[RowResult] = []
    entries: list[tuple[UUID, UUID | None, Decimal, Decimal]] = []
    seen: dict[tuple, int] = {}
    for row in rows:
        v = row.values
        result = RowResult(row.number, identity={"sku": v.get("sku") or None, "barcode": v.get("barcode") or None})
        results.append(result)
        if not v.get("sku") and not v.get("barcode"):
            result.error("sku or barcode is required")
            continue
        hit = by_sku.get(v["sku"]) if v.get("sku") else None
        hit_bc = by_barcode.get(v["barcode"]) if v.get("barcode") else None
        if v.get("sku") and hit is None:
            result.error(f"unknown sku '{v['sku']}'")
        if v.get("barcode") and hit_bc is None:
            result.error(f"unknown barcode '{v['barcode']}'")
        if hit and hit_bc and hit[:2] != hit_bc[:2]:
            result.error("sku and barcode belong to different products")
        if result.errors:
            continue
        target = hit or hit_bc
        assert target is not None
        result.details["product"] = target[2]
        if not v.get("price"):
            result.error("price is required")
            continue
        try:
            price = parse_decimal(v["price"], "price")
            min_qty = parse_decimal(v["min_qty"], "min_qty", minimum=None) if v.get("min_qty") else Decimal("1")
            if min_qty <= 0:
                raise ValueError("min_qty must be greater than 0")
        except ValueError as exc:
            result.error(str(exc))
            continue
        price = price.quantize(_CENT, rounding=ROUND_HALF_UP)
        key = (target[0], target[1], min_qty)
        if key in seen:
            result.error(f"duplicate of row {seen[key]} (same product and min_qty)")
            continue
        seen[key] = row.number
        old = current.get(key)
        result.details.update({"min_qty": str(min_qty), "old_price": None if old is None else str(old), "new_price": str(price)})
        if old is None:
            result.action = "create"
        elif old != price:
            result.action = "update"
        else:
            result.action = "unchanged"
        if result.action != "unchanged":
            entries.append((target[0], target[1], price, min_qty))

    has_errors = any(r.action == "error" for r in results)
    if body.dry_run:
        return build_report(results, unknown, dry_run=True)
    if has_errors and body.on_error == "abort":
        raise reject_with_report(build_report(results, unknown, dry_run=False))

    applied = 0
    if entries:
        created, updated, _ = await _upsert_entries(db, business_id, list_id, entries, user_id)
        applied = created + updated
        await _audit(
            db, business_id, user_id, "update", "price_list_items", list_id, None,
            {"csv_import": {"created": created, "updated": updated}},
        )
        await db.commit()
    return build_report(results, unknown, dry_run=False, applied=applied)
