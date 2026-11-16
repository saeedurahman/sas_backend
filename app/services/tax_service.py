"""Tax rate management services."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product
from app.models.sales import TaxRate
from app.services.tax_settings import TaxSettings, load_tax_settings
from app.schemas.sales import CreateTaxRateRequest, UpdateTaxRateRequest


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_tax_rates(
    db: AsyncSession,
    business_id: UUID,
) -> list[TaxRate]:
    result = await db.execute(
        select(TaxRate)
        .where(
            TaxRate.business_id == business_id,
            TaxRate.deleted_at.is_(None),
        )
        .order_by(TaxRate.name)
    )
    return list(result.scalars().all())


async def get_tax_rate_by_id(
    db: AsyncSession,
    tax_rate_id: UUID,
    business_id: UUID,
) -> TaxRate:
    result = await db.execute(
        select(TaxRate).where(
            TaxRate.id == tax_rate_id,
            TaxRate.business_id == business_id,
            TaxRate.deleted_at.is_(None),
        )
    )
    tax_rate = result.scalar_one_or_none()
    if tax_rate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tax rate not found",
        )
    return tax_rate


async def create_tax_rate(
    db: AsyncSession,
    business_id: UUID,
    data: CreateTaxRateRequest,
    created_by: UUID,
) -> TaxRate:
    now = _now()
    tax_rate = TaxRate(
        business_id=business_id,
        name=data.name,
        rate=data.rate,
        is_compound=data.is_compound,
        is_active=True,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(tax_rate)
    await db.commit()
    await db.refresh(tax_rate)
    return tax_rate


async def update_tax_rate(
    db: AsyncSession,
    tax_rate_id: UUID,
    business_id: UUID,
    data: UpdateTaxRateRequest,
    updated_by: UUID,
) -> TaxRate:
    tax_rate = await get_tax_rate_by_id(db, tax_rate_id, business_id)
    now = _now()

    if data.name is not None:
        tax_rate.name = data.name
    if data.rate is not None:
        tax_rate.rate = data.rate
    if data.is_active is not None:
        tax_rate.is_active = data.is_active

    tax_rate.updated_by = updated_by
    tax_rate.updated_at = now
    await db.commit()
    await db.refresh(tax_rate)
    return tax_rate


OVERRIDE_TAX_PERMISSION = "sales.override_tax"


async def resolve_product_tax_rates(
    db: AsyncSession,
    business_id: UUID,
    product_ids: list[UUID],
    cfg: TaxSettings | None = None,
) -> dict[UUID, Decimal]:
    """Tax rate (percent) per product.

    The product's own tax rate if it has one (0 when that rate is inactive), else the
    business default POS rate (``tax.default_rate_id``), else 0. Tax-exempt products
    and businesses with ``tax.enabled = false`` always get 0.
    """
    if not product_ids:
        return {}
    cfg = cfg or await load_tax_settings(db, business_id)
    zero = Decimal("0")
    if not cfg.enabled:
        return {pid: zero for pid in set(product_ids)}

    default_rate: Decimal | None = None
    if cfg.default_rate_id:
        found = (
            await db.execute(
                select(TaxRate.rate).where(
                    TaxRate.id == UUID(cfg.default_rate_id),
                    TaxRate.business_id == business_id,
                    TaxRate.is_active.is_(True),
                    TaxRate.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if found is not None:
            default_rate = Decimal(str(found))

    result = await db.execute(
        select(Product.id, Product.tax_rate_id, Product.is_tax_exempt, TaxRate.rate)
        .outerjoin(
            TaxRate,
            (TaxRate.id == Product.tax_rate_id)
            & (TaxRate.business_id == business_id)
            & (TaxRate.is_active.is_(True))
            & (TaxRate.deleted_at.is_(None)),
        )
        .where(Product.id.in_(set(product_ids)), Product.business_id == business_id)
    )
    rates: dict[UUID, Decimal] = {}
    for product_id, tax_rate_id, exempt, rate in result.all():
        if exempt:
            rates[product_id] = zero
        elif tax_rate_id is not None:
            rates[product_id] = Decimal(str(rate)) if rate is not None else zero
        else:
            rates[product_id] = default_rate if default_rate is not None else zero
    return rates


async def resolve_line_tax_rates(
    db: AsyncSession,
    business_id: UUID,
    lines: list,
    permission_keys: set[str],
    cfg: TaxSettings | None = None,
) -> list[Decimal]:
    """Server-side tax rate for each line (``lines[i].product_id`` / ``.tax_rate``).

    A client-supplied ``tax_rate`` is honoured only for users holding
    ``sales.override_tax``; otherwise it is ignored in favour of the product's
    configured rate. With ``tax.enabled = false`` every rate is 0.
    """
    cfg = cfg or await load_tax_settings(db, business_id)
    if not cfg.enabled:
        return [Decimal("0") for _ in lines]
    server = await resolve_product_tax_rates(
        db, business_id, [line.product_id for line in lines], cfg
    )
    may_override = OVERRIDE_TAX_PERMISSION in permission_keys
    rates: list[Decimal] = []
    for line in lines:
        requested = getattr(line, "tax_rate", None)
        if may_override and requested is not None:
            rates.append(Decimal(str(requested)))
        else:
            rates.append(server.get(line.product_id, Decimal("0")))
    return rates
