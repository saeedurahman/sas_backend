"""Receipt layouts: CRUD, duplication, channel assignments, resolution and preview."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from zoneinfo import ZoneInfo

from app.models.business import Branch, Business
from app.models.receipt_layout import ReceiptLayout, ReceiptLayoutAssignment
from app.schemas.invoice import (
    BusinessInfoForInvoice,
    CustomerInfoForInvoice,
    InvoiceData,
    InvoiceLineBatch,
    InvoiceLineItem,
    InvoicePaymentItem,
    InvoiceSummary,
    InvoiceTaxBreakdownItem,
)
from app.services.audit_helper import log_audit
from app.services.receipt_layout_config import (
    CHANNELS,
    DESIGN_NAMES,
    DESIGN_PAPER,
    DESIGNS,
    PAPERS,
    default_config,
    normalise_config,
)
from app.services.receipt_render import LayoutView

THERMAL_PAPERS = {"roll58", "roll80"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _unprocessable(message: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, message)


def check_design_paper(design: str, paper: str) -> None:
    if design not in DESIGNS:
        raise _unprocessable(f"design must be one of: {', '.join(DESIGNS)}")
    if paper not in PAPERS:
        raise _unprocessable(f"paper must be one of: {', '.join(PAPERS)}")
    if design.startswith("a4_") and paper != "a4":
        raise _unprocessable(f"design {design} needs paper a4")
    if design.startswith("thermal_") and paper not in THERMAL_PAPERS:
        raise _unprocessable(f"design {design} needs paper roll58 or roll80")


def read_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """A stored config as version 2 (old configs are upgraded on read; a corrupt one is returned as is)."""
    try:
        return normalise_config(raw)
    except (ValueError, TypeError):
        return raw or {}


def layout_dict(layout: ReceiptLayout) -> dict[str, Any]:
    return {
        "id": layout.id,
        "business_id": layout.business_id,
        "name": layout.name,
        "design": layout.design,
        "paper": layout.paper,
        "config": read_config(layout.config),
        "is_active": layout.is_active,
        "created_at": layout.created_at,
        "updated_at": layout.updated_at,
    }


def _audit_view(layout: ReceiptLayout) -> dict[str, Any]:
    return {
        "name": layout.name,
        "design": layout.design,
        "paper": layout.paper,
        "is_active": layout.is_active,
        "config": layout.config,
    }


def assignment_dict(row: ReceiptLayoutAssignment) -> dict[str, Any]:
    return {
        "id": row.id,
        "branch_id": row.branch_id,
        "channel": row.channel,
        "layout_id": row.layout_id,
        "updated_at": row.updated_at,
    }


# ── seeding ──────────────────────────────────────────────────────────────────


async def seed_default_layouts(db: AsyncSession, business_id: UUID, *, pharmacy: bool, actor: UUID | None = None) -> None:
    """Four layouts (one per design) and thermal_classic as the default of every channel.

    Adds rows only (no commit) and does nothing when the business already has layouts.
    """
    existing = (
        await db.execute(
            select(ReceiptLayout.id).where(ReceiptLayout.business_id == business_id).limit(1)
        )
    ).first()
    if existing is not None:
        return
    now = _now()
    classic: ReceiptLayout | None = None
    for design in DESIGNS:
        layout = ReceiptLayout(
            id=uuid.uuid4(),
            business_id=business_id,
            name=DESIGN_NAMES[design],
            design=design,
            paper=DESIGN_PAPER[design],
            config=default_config(design, pharmacy=pharmacy),
            is_active=True,
            created_at=now,
            updated_at=now,
            created_by=actor,
        )
        db.add(layout)
        if design == "thermal_classic":
            classic = layout
    await db.flush()
    for channel in CHANNELS:
        db.add(
            ReceiptLayoutAssignment(
                id=uuid.uuid4(),
                business_id=business_id,
                branch_id=None,
                channel=channel,
                layout_id=classic.id,
                created_at=now,
                updated_at=now,
                created_by=actor,
            )
        )
    await db.flush()


# ── layouts ──────────────────────────────────────────────────────────────────


async def list_layouts(db: AsyncSession, business_id: UUID) -> list[ReceiptLayout]:
    rows = await db.execute(
        select(ReceiptLayout)
        .where(ReceiptLayout.business_id == business_id, ReceiptLayout.deleted_at.is_(None))
        .order_by(ReceiptLayout.created_at, ReceiptLayout.name)
    )
    return list(rows.scalars().all())


async def get_layout(db: AsyncSession, business_id: UUID, layout_id: UUID, *, lock: bool = False) -> ReceiptLayout:
    stmt = select(ReceiptLayout).where(
        ReceiptLayout.id == layout_id,
        ReceiptLayout.business_id == business_id,
        ReceiptLayout.deleted_at.is_(None),
    )
    if lock:
        stmt = stmt.with_for_update()
    layout = (await db.execute(stmt)).scalar_one_or_none()
    if layout is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Layout not found")
    return layout


async def _assigned_channels(db: AsyncSession, business_id: UUID, layout_id: UUID) -> list[ReceiptLayoutAssignment]:
    rows = await db.execute(
        select(ReceiptLayoutAssignment).where(
            ReceiptLayoutAssignment.business_id == business_id,
            ReceiptLayoutAssignment.layout_id == layout_id,
            ReceiptLayoutAssignment.deleted_at.is_(None),
        )
    )
    return list(rows.scalars().all())


async def create_layout(
    db: AsyncSession, business_id: UUID, actor: UUID, *, name: str, design: str,
    paper: str | None, config: dict[str, Any] | None,
) -> ReceiptLayout:
    paper = paper or DESIGN_PAPER.get(design, "roll80")
    check_design_paper(design, paper)
    full = normalise_config(config) if config is not None else default_config(design)
    now = _now()
    layout = ReceiptLayout(
        id=uuid.uuid4(), business_id=business_id, name=name.strip(), design=design, paper=paper,
        config=full, is_active=True, created_at=now, updated_at=now, created_by=actor, updated_by=actor,
    )
    db.add(layout)
    await db.flush()
    await log_audit(
        db, business_id=business_id, user_id=actor, branch_id=None, action="create",
        table_name="receipt_layouts", record_id=layout.id, new_values=_audit_view(layout),
    )
    await db.commit()
    await db.refresh(layout)
    return layout


async def update_layout(
    db: AsyncSession, business_id: UUID, actor: UUID, layout_id: UUID, changes: dict[str, Any]
) -> ReceiptLayout:
    layout = await get_layout(db, business_id, layout_id, lock=True)
    before = _audit_view(layout)
    paper = changes.get("paper", layout.paper)
    check_design_paper(layout.design, paper)
    if "name" in changes and changes["name"] is not None:
        layout.name = changes["name"].strip()
    layout.paper = paper
    if changes.get("config") is not None:
        layout.config = normalise_config(changes["config"])
    if changes.get("is_active") is not None:
        if changes["is_active"] is False and layout.is_active:
            if await _assigned_channels(db, business_id, layout.id):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "This layout is assigned to a channel; assign another layout first",
                )
        layout.is_active = changes["is_active"]
    layout.updated_at = _now()
    layout.updated_by = actor
    await log_audit(
        db, business_id=business_id, user_id=actor, branch_id=None, action="update",
        table_name="receipt_layouts", record_id=layout.id, old_values=before, new_values=_audit_view(layout),
    )
    await db.commit()
    await db.refresh(layout)
    return layout


async def delete_layout(db: AsyncSession, business_id: UUID, actor: UUID, layout_id: UUID) -> None:
    layout = await get_layout(db, business_id, layout_id, lock=True)
    if await _assigned_channels(db, business_id, layout.id):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This layout is assigned to a channel; assign another layout first"
        )
    before = _audit_view(layout)
    now = _now()
    layout.deleted_at = now
    layout.deleted_by = actor
    layout.updated_at = now
    await log_audit(
        db, business_id=business_id, user_id=actor, branch_id=None, action="delete",
        table_name="receipt_layouts", record_id=layout.id, old_values=before,
    )
    await db.commit()


async def duplicate_layout(
    db: AsyncSession, business_id: UUID, actor: UUID, layout_id: UUID, name: str | None
) -> ReceiptLayout:
    source = await get_layout(db, business_id, layout_id)
    return await create_layout(
        db, business_id, actor,
        name=(name or f"{source.name} (copy)")[:100], design=source.design, paper=source.paper,
        config=source.config,
    )


# ── assignments ──────────────────────────────────────────────────────────────


async def list_assignments(db: AsyncSession, business_id: UUID) -> list[ReceiptLayoutAssignment]:
    rows = await db.execute(
        select(ReceiptLayoutAssignment)
        .where(
            ReceiptLayoutAssignment.business_id == business_id,
            ReceiptLayoutAssignment.deleted_at.is_(None),
        )
        .order_by(ReceiptLayoutAssignment.channel, ReceiptLayoutAssignment.created_at)
    )
    return list(rows.scalars().all())


async def put_assignments(
    db: AsyncSession, business_id: UUID, actor: UUID, items: list[dict[str, Any]]
) -> list[ReceiptLayoutAssignment]:
    now = _now()
    seen: set[tuple[UUID | None, str]] = set()
    for item in items:
        channel, branch_id, layout_id = item["channel"], item.get("branch_id"), item.get("layout_id")
        if channel not in CHANNELS:
            raise _unprocessable(f"channel must be one of: {', '.join(CHANNELS)}")
        if (branch_id, channel) in seen:
            raise _unprocessable("duplicate scope in request")
        seen.add((branch_id, channel))
        if branch_id is not None:
            found = (
                await db.execute(
                    select(Branch.id).where(
                        Branch.id == branch_id, Branch.business_id == business_id, Branch.deleted_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
            if found is None:
                raise _unprocessable("branch not found")
        existing = (
            await db.execute(
                select(ReceiptLayoutAssignment)
                .where(
                    ReceiptLayoutAssignment.business_id == business_id,
                    ReceiptLayoutAssignment.channel == channel,
                    ReceiptLayoutAssignment.deleted_at.is_(None),
                    (ReceiptLayoutAssignment.branch_id.is_(None) if branch_id is None
                     else ReceiptLayoutAssignment.branch_id == branch_id),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if layout_id is None:
            if branch_id is None:
                raise _unprocessable("the business default cannot be removed; assign another layout instead")
            if existing is not None:
                await log_audit(
                    db, business_id=business_id, user_id=actor, branch_id=branch_id, action="delete",
                    table_name="receipt_layout_assignments", record_id=existing.id,
                    old_values={"channel": channel, "branch_id": str(branch_id), "layout_id": str(existing.layout_id)},
                )
                existing.deleted_at = now
                existing.deleted_by = actor
                existing.updated_at = now
            continue
        layout = await get_layout(db, business_id, layout_id)
        if not layout.is_active:
            raise _unprocessable("an inactive layout cannot be assigned")
        if existing is not None:
            if existing.layout_id != layout_id:
                await log_audit(
                    db, business_id=business_id, user_id=actor, branch_id=branch_id, action="update",
                    table_name="receipt_layout_assignments", record_id=existing.id,
                    old_values={"channel": channel, "branch_id": str(branch_id) if branch_id else None,
                                "layout_id": str(existing.layout_id)},
                    new_values={"channel": channel, "branch_id": str(branch_id) if branch_id else None,
                                "layout_id": str(layout_id)},
                )
                existing.layout_id = layout_id
                existing.updated_at = now
                existing.updated_by = actor
        else:
            row = ReceiptLayoutAssignment(
                id=uuid.uuid4(), business_id=business_id, branch_id=branch_id, channel=channel,
                layout_id=layout_id, created_at=now, updated_at=now, created_by=actor,
            )
            db.add(row)
            await db.flush()
            await log_audit(
                db, business_id=business_id, user_id=actor, branch_id=branch_id, action="create",
                table_name="receipt_layout_assignments", record_id=row.id,
                new_values={"channel": channel, "branch_id": str(branch_id) if branch_id else None,
                            "layout_id": str(layout_id)},
            )
    await db.commit()
    return await list_assignments(db, business_id)


# ── resolution ───────────────────────────────────────────────────────────────


def view_of(layout: ReceiptLayout) -> LayoutView:
    return LayoutView.build(str(layout.id), layout.name, layout.design, layout.paper, layout.config)


def fallback_view() -> LayoutView:
    """Used only when a business has no usable assignment (should not happen after seeding)."""
    return LayoutView.build(None, DESIGN_NAMES["thermal_classic"], "thermal_classic", "roll80",
                            default_config("thermal_classic"))


async def resolve_layout(
    db: AsyncSession, business_id: UUID, branch_id: UUID | None, channel: str
) -> LayoutView:
    """Branch assignment, else the business default, else the built-in classic layout."""
    scopes = [branch_id, None] if branch_id is not None else [None]
    for scope in scopes:
        row = (
            await db.execute(
                select(ReceiptLayout)
                .join(ReceiptLayoutAssignment, ReceiptLayoutAssignment.layout_id == ReceiptLayout.id)
                .where(
                    ReceiptLayoutAssignment.business_id == business_id,
                    ReceiptLayoutAssignment.channel == channel,
                    ReceiptLayoutAssignment.deleted_at.is_(None),
                    ReceiptLayout.deleted_at.is_(None),
                    ReceiptLayout.is_active.is_(True),
                    (ReceiptLayoutAssignment.branch_id.is_(None) if scope is None
                     else ReceiptLayoutAssignment.branch_id == scope),
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            return view_of(row)
    return fallback_view()


# ── preview ──────────────────────────────────────────────────────────────────

SAMPLE_LINK = "https://example.com/r/sample-receipt"


def mock_sale(business: Business | None, *, tax_label: str = "GST") -> InvoiceData:
    """A built-in sample sale: Urdu names, a batch with expiry, tax, discount and cash change."""
    now = datetime.now(timezone.utc)
    expiry = date.today() + timedelta(days=420)
    lines = [
        InvoiceLineItem(
            line_order=0, product_name="Panadol Extra 500mg", product_name_secondary="پینا ڈول ایکسٹرا",
            sku="PAN-500", barcode="6001234567890", unit="strip", unit_allows_decimal=False,
            batches=[InvoiceLineBatch(batch_no="B2407", expiry_date=expiry, qty=Decimal("2"))],
            qty=Decimal("2"), unit_price=Decimal("120.00"), discount_pct=Decimal("0"),
            discount_amount=Decimal("0"), tax_rate=Decimal("17"), net_amount=Decimal("240.00"),
            tax_amount=Decimal("40.80"), line_total=Decimal("280.80"),
        ),
        InvoiceLineItem(
            line_order=1, product_name="Basmati Rice", product_name_secondary="باسمتی چاول",
            variation_name="5 kg", sku="RICE-5", unit="kg", unit_allows_decimal=True,
            qty=Decimal("1.5"), unit_price=Decimal("400.00"), discount_pct=Decimal("0"),
            discount_amount=Decimal("20.00"), tax_rate=Decimal("17"), net_amount=Decimal("580.00"),
            tax_amount=Decimal("98.60"), line_total=Decimal("678.60"),
        ),
    ]
    subtotal = Decimal("840.00")
    total = Decimal("959.40")
    summary = InvoiceSummary(
        tax_inclusive=False, tax_label=tax_label, tax_rounding_mode="nearest_5", show_tax_breakdown=True,
        subtotal=subtotal, total_discount=Decimal("20.00"), net_amount=Decimal("820.00"),
        total_tax=Decimal("139.40"), exact_total=Decimal("959.40"), rounding_amount=Decimal("0.60"),
        total_amount=Decimal("960.00"),
        tax_breakdown=[InvoiceTaxBreakdownItem(tax_label=tax_label, tax_rate=Decimal("17"),
                                               net_amount=Decimal("820.00"), tax_amount=Decimal("139.40"))],
        total_paid=Decimal("1000.00"), balance_due=Decimal("0"), change_given=Decimal("40.00"),
    )
    del total
    return InvoiceData(
        sale_id=uuid.uuid4(), sale_number="INV-0001042", client_receipt_no="C1-0087", sale_type="pos",
        status="completed", sold_at=now, branch_id=uuid.uuid4(), branch_name="Main Branch",
        branch_address=(business.address_line1 if business and business.address_line1 else "Shop 12, Main Bazaar, Lahore"),
        branch_phone=(business.phone if business and business.phone else "0300-1234567"),
        cashier_name="Ali", business=BusinessInfoForInvoice(
            name=business.name if business else "Sample Store", logo_url=None,
            address=business.address_line1 if business else None, phone=business.phone if business else None,
            tax_id=(business.tax_id if business and business.tax_id else "1234567-8"),
            city=business.city if business else None,
            currency=(business.currency_code if business else "PKR"),
        ),
        customer=CustomerInfoForInvoice(name="احمد خان", phone="0321-7654321"),
        lines=lines,
        payments=[InvoicePaymentItem(payment_method="cash", amount=Decimal("960.00"), paid_at=now)],
        summary=summary, receipt_footer=None, printed_at=now, register_name="Counter 1",
        customer_previous_balance=Decimal("250.00"), customer_balance_after=Decimal("250.00"),
        synced=True,
    )
