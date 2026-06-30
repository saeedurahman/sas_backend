"""Stock ledger and balance services.

Document numbers use document_number_counters (atomic daily sequences).
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.models.business import Branch, BusinessConfig
from app.models.enums import ReferenceTypeEnum, StockMovementTypeEnum
from app.models.inventory import StockMovement
from app.models.product import Product, ProductVariation
from app.schemas.inventory import StockBalanceResponse


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def generate_document_number(
    db: AsyncSession,
    business_id: UUID,
    prefix: str,
    model: type,
    number_column: InstrumentedAttribute,
) -> str:
    """Return the next `{prefix}-YYYYMMDD-####` for this business and day.

    Uses document_number_counters with INSERT ... ON CONFLICT DO UPDATE for
    atomic increment under concurrency. ``model`` and ``number_column`` are
    retained for call-site compatibility only.
    """
    del model, number_column
    date_str = _now().strftime("%Y%m%d")
    result = await db.execute(
        text(
            """
            INSERT INTO document_number_counters (
                id, business_id, prefix, date_key, last_sequence
            ) VALUES (
                gen_random_uuid(), :business_id, :prefix, :date_key, 1
            )
            ON CONFLICT (business_id, prefix, date_key)
            DO UPDATE SET
                last_sequence = document_number_counters.last_sequence + 1
            RETURNING last_sequence
            """
        ),
        {
            "business_id": business_id,
            "prefix": prefix,
            "date_key": date_str,
        },
    )
    seq = result.scalar_one()
    return f"{prefix}-{date_str}-{seq:04d}"


async def verify_branch(
    db: AsyncSession,
    branch_id: UUID,
    business_id: UUID,
) -> Branch:
    result = await db.execute(
        select(Branch).where(
            Branch.id == branch_id,
            Branch.business_id == business_id,
            Branch.deleted_at.is_(None),
        )
    )
    branch = result.scalar_one_or_none()
    if branch is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Branch not found",
        )
    return branch


async def verify_product(
    db: AsyncSession,
    product_id: UUID,
    business_id: UUID,
) -> Product:
    result = await db.execute(
        select(Product).where(
            Product.id == product_id,
            Product.business_id == business_id,
            Product.deleted_at.is_(None),
        )
    )
    product = result.scalar_one_or_none()
    if product is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Product not found",
        )
    return product


async def verify_variation(
    db: AsyncSession,
    variation_id: UUID,
    product_id: UUID,
    business_id: UUID,
) -> ProductVariation:
    result = await db.execute(
        select(ProductVariation).where(
            ProductVariation.id == variation_id,
            ProductVariation.product_id == product_id,
            ProductVariation.business_id == business_id,
            ProductVariation.deleted_at.is_(None),
        )
    )
    variation = result.scalar_one_or_none()
    if variation is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Variation not found for product",
        )
    return variation


async def get_allow_negative_stock(
    db: AsyncSession,
    business_id: UUID,
) -> bool:
    result = await db.execute(
        select(BusinessConfig.allow_negative_stock).where(
            BusinessConfig.business_id == business_id,
            BusinessConfig.deleted_at.is_(None),
        )
    )
    value = result.scalar_one_or_none()
    return bool(value) if value is not None else False


async def get_stock_balance(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID,
    product_id: UUID,
    variation_id: UUID | None = None,
) -> Decimal:
    filters = [
        StockMovement.business_id == business_id,
        StockMovement.branch_id == branch_id,
        StockMovement.product_id == product_id,
        StockMovement.deleted_at.is_(None),
    ]
    if variation_id is None:
        filters.append(StockMovement.variation_id.is_(None))
    else:
        filters.append(StockMovement.variation_id == variation_id)

    result = await db.execute(
        select(func.coalesce(func.sum(StockMovement.qty), 0)).where(*filters)
    )
    total = result.scalar_one()
    return Decimal(str(total))


async def get_stock_balance_detail(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID,
    product_id: UUID,
    variation_id: UUID | None = None,
) -> StockBalanceResponse:
    filters = [
        StockMovement.business_id == business_id,
        StockMovement.branch_id == branch_id,
        StockMovement.product_id == product_id,
        StockMovement.deleted_at.is_(None),
    ]
    if variation_id is None:
        filters.append(StockMovement.variation_id.is_(None))
    else:
        filters.append(StockMovement.variation_id == variation_id)

    result = await db.execute(
        select(
            func.coalesce(func.sum(StockMovement.qty), 0),
            func.max(StockMovement.movement_at),
        ).where(*filters)
    )
    row = result.one()
    return StockBalanceResponse(
        product_id=product_id,
        variation_id=variation_id,
        branch_id=branch_id,
        current_qty=Decimal(str(row[0])),
        last_movement_at=row[1],
    )


async def get_stock_balances_for_branch(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID,
    product_ids: list[UUID] | None = None,
) -> list[StockBalanceResponse]:
    filters = [
        StockMovement.business_id == business_id,
        StockMovement.branch_id == branch_id,
        StockMovement.deleted_at.is_(None),
    ]
    if product_ids:
        filters.append(StockMovement.product_id.in_(product_ids))

    result = await db.execute(
        select(
            StockMovement.product_id,
            StockMovement.variation_id,
            func.coalesce(func.sum(StockMovement.qty), 0),
            func.max(StockMovement.movement_at),
        )
        .where(*filters)
        .group_by(StockMovement.product_id, StockMovement.variation_id)
    )
    return [
        StockBalanceResponse(
            product_id=row[0],
            variation_id=row[1],
            branch_id=branch_id,
            current_qty=Decimal(str(row[2])),
            last_movement_at=row[3],
        )
        for row in result.all()
    ]


async def get_stock_movements(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
    product_id: UUID | None = None,
    movement_type: str | None = None,
    skip: int = 0,
    limit: int = 50,
) -> tuple[list[StockMovement], int]:
    filters = [
        StockMovement.business_id == business_id,
        StockMovement.deleted_at.is_(None),
    ]
    if branch_id is not None:
        filters.append(StockMovement.branch_id == branch_id)
    if product_id is not None:
        filters.append(StockMovement.product_id == product_id)
    if movement_type is not None:
        filters.append(StockMovement.movement_type == movement_type)

    count_result = await db.execute(
        select(func.count()).select_from(StockMovement).where(*filters)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(StockMovement)
        .where(*filters)
        .order_by(StockMovement.movement_at.desc())
        .offset(skip)
        .limit(limit)
    )
    return list(result.scalars().all()), total


async def create_stock_movement(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID,
    product_id: UUID,
    variation_id: UUID | None,
    movement_type: StockMovementTypeEnum,
    qty: Decimal,
    cost_per_unit: Decimal,
    reference_type: ReferenceTypeEnum,
    reference_id: UUID,
    created_by: UUID,
    purchase_line_id: UUID | None = None,
    batch_number: str | None = None,
    expiry_date: date | None = None,
    notes: str | None = None,
    movement_at: datetime | None = None,
    movement_sequence: int | None = None,
) -> StockMovement:
    now = _now()
    movement = StockMovement(
        business_id=business_id,
        branch_id=branch_id,
        product_id=product_id,
        variation_id=variation_id,
        movement_type=movement_type.value,
        qty=qty,
        cost_per_unit=cost_per_unit,
        reference_type=reference_type.value,
        reference_id=reference_id,
        purchase_line_id=purchase_line_id,
        batch_number=batch_number,
        expiry_date=expiry_date,
        notes=notes,
        movement_at=movement_at or now,
        movement_sequence=movement_sequence,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(movement)
    await db.flush()
    return movement


async def check_sufficient_stock(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID,
    product_id: UUID,
    variation_id: UUID | None,
    required_qty: Decimal,
) -> bool:
    balance = await get_stock_balance(
        db, business_id, branch_id, product_id, variation_id
    )
    return balance >= required_qty
