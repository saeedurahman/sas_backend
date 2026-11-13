"""Price list management services."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload, selectinload

from app.models.product import PriceList, PriceListItem, Product
from app.models.sales import Customer
from app.services.audit_helper import log_audit
from app.schemas.product import CreatePriceListRequest, SetPriceRequest


DEFAULT_PRICE_LIST_NAME = "Retail"


async def ensure_default_price_list(
    db: AsyncSession,
    business_id: UUID,
    created_by: UUID | None = None,
) -> UUID:
    """Return the business's default price list id, creating it if missing.

    Race-safe: a transaction-scoped advisory lock per business serialises
    concurrent creators, and the partial unique index ``uq_price_lists_one_default``
    is the backstop. Flushes but never commits - the caller owns the transaction
    (so it can be used inside sale/sync transactions).
    """
    find_default = select(PriceList.id).where(
        PriceList.business_id == business_id,
        PriceList.deleted_at.is_(None),
        PriceList.is_default.is_(True),
    )
    existing = (await db.execute(find_default)).scalar_one_or_none()
    if existing is not None:
        return existing

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"default_price_list:{business_id}"},
    )
    existing = (await db.execute(find_default)).scalar_one_or_none()
    if existing is not None:
        return existing

    now = datetime.now(timezone.utc)

    # Prefer promoting a live, active retail list the owner already made.
    candidate = (
        await db.execute(
            select(PriceList)
            .where(
                PriceList.business_id == business_id,
                PriceList.deleted_at.is_(None),
                PriceList.is_active.is_(True),
                PriceList.list_type == "retail",
            )
            .order_by(PriceList.created_at, PriceList.id)
            .limit(1)
            .options(noload(PriceList.items))
        )
    ).scalar_one_or_none()
    if candidate is not None:
        candidate.is_default = True
        candidate.updated_at = now
        await db.flush()
        return candidate.id

    # (business_id, name) is unique even across soft-deleted rows.
    taken = set(
        (
            await db.execute(
                select(PriceList.name).where(PriceList.business_id == business_id)
            )
        ).scalars()
    )
    name = DEFAULT_PRICE_LIST_NAME
    if name in taken:
        name = f"{DEFAULT_PRICE_LIST_NAME} Default"
    if name in taken:
        name = f"{DEFAULT_PRICE_LIST_NAME} Default {str(business_id)[:8]}"

    price_list = PriceList(
        business_id=business_id,
        name=name,
        list_type="retail",
        is_default=True,
        is_active=True,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(price_list)
    await db.flush()
    return price_list.id


async def with_counts(
    db: AsyncSession, business_id: UUID, lists: list[PriceList]
) -> list[PriceList]:
    """Attach ``item_count`` / ``customer_count`` (read by PriceListResponse)."""
    ids = [pl.id for pl in lists]
    items: dict = {}
    customers: dict = {}
    if ids:
        items = dict(
            (
                await db.execute(
                    select(PriceListItem.price_list_id, func.count())
                    .where(
                        PriceListItem.business_id == business_id,
                        PriceListItem.price_list_id.in_(ids),
                        PriceListItem.deleted_at.is_(None),
                    )
                    .group_by(PriceListItem.price_list_id)
                )
            ).all()
        )
        customers = dict(
            (
                await db.execute(
                    select(Customer.price_list_id, func.count())
                    .where(
                        Customer.business_id == business_id,
                        Customer.price_list_id.in_(ids),
                        Customer.deleted_at.is_(None),
                    )
                    .group_by(Customer.price_list_id)
                )
            ).all()
        )
    for pl in lists:
        pl.item_count = items.get(pl.id, 0)  # type: ignore[attr-defined]
        pl.customer_count = customers.get(pl.id, 0)  # type: ignore[attr-defined]
    return lists


async def get_price_lists(
    db: AsyncSession,
    business_id: UUID,
) -> list[PriceList]:
    # Lazy safety net: a business must always have a default list to price against.
    await ensure_default_price_list(db, business_id)
    await db.commit()
    result = await db.execute(
        select(PriceList)
        .where(
            PriceList.business_id == business_id,
            PriceList.deleted_at.is_(None),
        )
        .options(noload(PriceList.items))
        .order_by(PriceList.is_default.desc(), PriceList.name)
    )
    return await with_counts(db, business_id, list(result.scalars().unique().all()))


async def create_price_list(
    db: AsyncSession,
    business_id: UUID,
    data: CreatePriceListRequest,
    created_by: UUID,
) -> PriceList:
    now = datetime.now(timezone.utc)
    if data.is_default and (data.valid_from is not None or data.valid_to is not None):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The default price list cannot have a validity window",
        )

    try:
        if data.is_default:
            existing = await db.execute(
                select(PriceList).where(
                    PriceList.business_id == business_id,
                    PriceList.deleted_at.is_(None),
                    PriceList.is_default.is_(True),
                )
            )
            for price_list in existing.scalars().all():
                price_list.is_default = False
                price_list.updated_by = created_by
                price_list.updated_at = now
            await db.flush()  # the unique index is immediate: demote before the new default

        price_list = PriceList(
            business_id=business_id,
            name=data.name,
            list_type=data.list_type.value,
            is_default=data.is_default,
            is_active=True,
            valid_from=data.valid_from,
            valid_to=data.valid_to,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(price_list)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Price list name already exists or a default list was just created",
        )
    except Exception:
        await db.rollback()
        raise

    await db.refresh(price_list)
    created = await _get_price_list_by_id(db, price_list.id, business_id)
    return (await with_counts(db, business_id, [created]))[0]


async def _get_price_list_by_id(
    db: AsyncSession,
    price_list_id: UUID,
    business_id: UUID,
) -> PriceList:
    result = await db.execute(
        select(PriceList)
        .where(
            PriceList.id == price_list_id,
            PriceList.business_id == business_id,
            PriceList.deleted_at.is_(None),
        )
        .options(noload(PriceList.items))
    )
    price_list = result.scalar_one_or_none()
    if price_list is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Price list not found",
        )
    return price_list


async def _verify_product(
    db: AsyncSession,
    product_id: UUID,
    business_id: UUID,
) -> None:
    result = await db.execute(
        select(Product.id).where(
            Product.id == product_id,
            Product.business_id == business_id,
            Product.deleted_at.is_(None),
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Product not found",
        )


async def set_price(
    db: AsyncSession,
    price_list_id: UUID,
    business_id: UUID,
    data: SetPriceRequest,
    created_by: UUID,
) -> PriceListItem:
    await _get_price_list_by_id(db, price_list_id, business_id)
    await _verify_product(db, data.product_id, business_id)

    if data.unit_price < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Price cannot be negative",
        )

    now = datetime.now(timezone.utc)
    stmt = select(PriceListItem).where(
        PriceListItem.price_list_id == price_list_id,
        PriceListItem.product_id == data.product_id,
        PriceListItem.business_id == business_id,
        PriceListItem.min_qty == data.min_qty,
    )
    if data.variation_id is None:
        stmt = stmt.where(PriceListItem.variation_id.is_(None))
    else:
        stmt = stmt.where(PriceListItem.variation_id == data.variation_id)

    # Soft-deleted rows still hold the unique key: revive one instead of inserting a twin.
    result = await db.execute(stmt.order_by(PriceListItem.deleted_at.is_(None).desc()))
    item = result.scalars().first()

    old_price = item.unit_price if item is not None and item.deleted_at is None else None
    if item is not None:
        item.unit_price = data.unit_price
        item.min_qty = data.min_qty
        item.deleted_at = None
        item.deleted_by = None
        item.updated_by = created_by
        item.updated_at = now
    else:
        item = PriceListItem(
            business_id=business_id,
            price_list_id=price_list_id,
            product_id=data.product_id,
            variation_id=data.variation_id,
            unit_price=data.unit_price,
            min_qty=data.min_qty,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(item)
        await db.flush()

    await log_audit(
        db,
        business_id=business_id,
        user_id=created_by,
        branch_id=None,  # price lists are business-wide
        action="update" if old_price is not None else "create",
        table_name="price_list_items",
        record_id=item.id,
        old_values=None if old_price is None else {"unit_price": str(old_price)},
        new_values={
            "price_list_id": str(price_list_id),
            "product_id": str(data.product_id),
            "variation_id": str(data.variation_id) if data.variation_id else None,
            "unit_price": str(data.unit_price),
            "min_qty": str(data.min_qty),
        },
    )
    await db.commit()
    await db.refresh(item)
    return item


def _usable(price_list: PriceList, now: datetime) -> bool:
    """Live, active and inside its validity window (the default list is always in window)."""
    if price_list.deleted_at is not None or not price_list.is_active:
        return False
    if price_list.is_default:
        return True
    if price_list.valid_from is not None and price_list.valid_from > now:
        return False
    return price_list.valid_to is None or price_list.valid_to > now


async def _item_price(
    db: AsyncSession,
    business_id: UUID,
    price_list_id: UUID,
    product_id: UUID,
    variation_id: UUID | None,
    qty: Decimal,
) -> Decimal | None:
    stmt = select(PriceListItem.unit_price).where(
        PriceListItem.price_list_id == price_list_id,
        PriceListItem.product_id == product_id,
        PriceListItem.business_id == business_id,
        PriceListItem.deleted_at.is_(None),
        PriceListItem.min_qty <= qty,
    )
    if variation_id is None:
        stmt = stmt.where(PriceListItem.variation_id.is_(None))
    else:
        stmt = stmt.where(PriceListItem.variation_id == variation_id)
    # Quantity tiers: the highest min_qty the quantity reaches wins.
    return (
        await db.execute(stmt.order_by(PriceListItem.min_qty.desc()).limit(1))
    ).scalar_one_or_none()


async def get_customer_price_list_id(
    db: AsyncSession, business_id: UUID, customer_id: UUID | None
) -> UUID | None:
    if customer_id is None:
        return None
    return (
        await db.execute(
            select(Customer.price_list_id).where(
                Customer.id == customer_id,
                Customer.business_id == business_id,
                Customer.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def get_product_price(
    db: AsyncSession,
    business_id: UUID,
    product_id: UUID,
    variation_id: UUID | None = None,
    price_list_id: UUID | None = None,
    *,
    customer_id: UUID | None = None,
    qty: Decimal = Decimal("1"),
) -> Decimal | None:
    """Catalog price for a product.

    Rule: use ``price_list_id`` when given, else the customer's assigned list, and fall back
    to the business default list whenever that list is unusable (inactive, outside its
    validity window) or has no price for the product.
    """
    now = datetime.now(timezone.utc)
    preferred_id = price_list_id
    if preferred_id is not None:
        await _get_price_list_by_id(db, preferred_id, business_id)  # 404 on a foreign id
    else:
        preferred_id = await get_customer_price_list_id(db, business_id, customer_id)
    default_id = await ensure_default_price_list(db, business_id)

    if preferred_id is not None and preferred_id != default_id:
        preferred = (
            await db.execute(
                select(PriceList)
                .where(PriceList.id == preferred_id, PriceList.business_id == business_id)
                .options(noload(PriceList.items))
            )
        ).scalar_one_or_none()
        if preferred is not None and _usable(preferred, now):
            price = await _item_price(
                db, business_id, preferred_id, product_id, variation_id, qty
            )
            if price is not None:
                return price
    return await _item_price(db, business_id, default_id, product_id, variation_id, qty)


async def apply_sale_price_rules(db: AsyncSession, business_id: UUID, data) -> None:
    """Pricing rule for a new sale (mutates ``data``).

    * No ``price_list_id`` but a customer with an assigned list: that list is used (and
      recorded on the sale).
    * A line without ``unit_price`` is priced from that list, falling back to the default.
    """
    if data.price_list_id is None and data.customer_id is not None:
        data.price_list_id = await get_customer_price_list_id(
            db, business_id, data.customer_id
        )
    for line in data.lines:
        if line.unit_price is not None:
            continue
        price = await get_product_price(
            db, business_id, line.product_id, line.variation_id, data.price_list_id,
            qty=line.qty,
        )
        if price is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"No price found for product {line.product_id}: set a price or send unit_price",
            )
        line.unit_price = price


async def validate_customer_price_list(
    db: AsyncSession, business_id: UUID, price_list_id: UUID | None
) -> None:
    """A customer's list must be a live, active list of the same business."""
    if price_list_id is None:
        return
    found = (
        await db.execute(
            select(PriceList.id).where(
                PriceList.id == price_list_id,
                PriceList.business_id == business_id,
                PriceList.deleted_at.is_(None),
                PriceList.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Price list not found or not active",
        )
