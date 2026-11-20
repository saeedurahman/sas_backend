"""Quick Add by scan: barcode lookup + bulk product/stock creation.

Nothing here has inventory or costing logic of its own. Products are created by
``product_service.create_product_in_session``, prices by ``price_service.upsert_price_item``
(default Retail list), stock by the existing services: ``create_stock_adjustment``
(``opening_balance``, FIFO layers with cost, batch/expiry) or ``create_purchase_order`` +
``create_purchase_receipt`` (GRN: FIFO layers, batch/expiry, supplier payable).

Two phases
----------
1. Every line runs in its **own savepoint** (``begin_nested``). A bad line becomes
   ``status: "error"`` and is rolled back on its own; the other lines are unaffected. The product
   work (products, variations, barcodes, price-list items, audit rows) is then committed.
2. The stock of all lines with ``qty > 0`` is posted in ONE document (one adjustment, or one PO +
   one GRN). Those services validate and commit as a unit. If posting fails (not expected: every
   line was validated in phase 1) the products stay and the affected lines come back as
   ``error`` / ``stock_failed`` with their ``product_id``; re-scanning them adds stock to the
   existing product (retry with a NEW Idempotency-Key, the failed response was stored).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.business import Branch
from app.models.enums import AdjustmentReasonEnum, ProductTypeEnum, TrackingTypeEnum
from app.models.product import Barcode, Brand, Category, Product, Unit
from app.models.sales import TaxRate
from app.schemas.inventory import (
    CreateAdjustmentLineRequest,
    CreatePurchaseLineRequest,
    CreatePurchaseOrderRequest,
    CreatePurchaseReceiptRequest,
    CreateReceiptLineRequest,
    CreateStockAdjustmentRequest,
)
from app.schemas.product import CreateProductRequest, NewBarcodeInput
from app.schemas.quick_add import (
    CatalogSuggestion,
    LookupBarcodeInfo,
    LookupProduct,
    LookupUnit,
    ProductLookupResponse,
    QuickAddLine,
    QuickAddLineError,
    QuickAddLineResult,
    QuickAddRequest,
    QuickAddResponse,
)
from app.services import quick_add_settings
from app.services.adjustment_service import create_stock_adjustment
from app.services.audit_helper import log_audit
from app.services.barcode_utils import match_candidates, normalize_barcode
from app.services.fifo_service import get_fallback_cost_per_unit
from app.services.price_service import ensure_default_price_list, upsert_price_item
from app.services.product_import_service import _parse_expiry
from app.services.product_service import create_product_in_session
from app.services.purchase_service import (
    create_purchase_order,
    create_purchase_receipt,
    update_purchase_order_status,
)
from app.services.supplier_service import get_supplier_by_id

log = logging.getLogger(__name__)

_CENT = Decimal("0.01")
_QTY_STEP = Decimal("0.0001")
_MAX_MONEY = Decimal("999999999.99")
_MAX_QTY = Decimal("999999999.9999")
DEFAULT_UNIT_NAME = "Piece"
DEFAULT_UNIT_SYMBOL = "pc"


# ═════════════════════════════════════════════════════════════════════════════
# GET /products/lookup
# ═════════════════════════════════════════════════════════════════════════════

_LOOKUP_SQL = text(
    """
    SELECT b.barcode AS matched_barcode, b.product_id, b.variation_id AS barcode_variation_id,
           p.name, p.track_expiry, p.image_url, p.base_unit_id,
           u.name AS unit_name, u.symbol AS unit_symbol,
           COALESCE(b.variation_id, dv.id) AS variation_id,
           price.unit_price AS sale_price,
           COALESCE(stock.qty, 0) AS stock_on_hand
    FROM barcodes b
    JOIN products p ON p.id = b.product_id AND p.business_id = b.business_id AND p.deleted_at IS NULL
    LEFT JOIN units u ON u.id = p.base_unit_id
    LEFT JOIN LATERAL (
        SELECT v.id FROM product_variations v
        WHERE v.business_id = b.business_id AND v.product_id = p.id AND v.deleted_at IS NULL
        ORDER BY v.is_default DESC, v.created_at LIMIT 1
    ) dv ON TRUE
    LEFT JOIN LATERAL (
        SELECT pli.unit_price FROM price_list_items pli
        JOIN price_lists pl ON pl.id = pli.price_list_id AND pl.is_default AND pl.deleted_at IS NULL
        WHERE pli.business_id = b.business_id AND pli.product_id = p.id AND pli.deleted_at IS NULL
          AND pli.min_qty <= 1
          AND (pli.variation_id IS NULL OR pli.variation_id = COALESCE(b.variation_id, dv.id))
        ORDER BY (pli.variation_id IS NULL), pli.min_qty DESC LIMIT 1
    ) price ON TRUE
    LEFT JOIN LATERAL (
        SELECT SUM(sl.qty) AS qty FROM stock_levels sl
        WHERE sl.business_id = b.business_id AND sl.product_id = p.id AND sl.deleted_at IS NULL
          AND (CAST(:branches AS uuid[]) IS NULL OR sl.branch_id = ANY(CAST(:branches AS uuid[])))
    ) stock ON TRUE
    WHERE b.business_id = :business_id AND b.deleted_at IS NULL
      AND b.barcode = ANY(CAST(:candidates AS text[]))
    ORDER BY array_position(CAST(:candidates AS text[]), b.barcode)
    LIMIT 1
    """
)


async def lookup_barcode(
    db: AsyncSession,
    business_id: UUID,
    barcode: str,
    branch_ids: list[UUID] | None,
) -> ProductLookupResponse:
    """One indexed query on ``barcodes (business_id, barcode)`` + lateral joins; a miss costs one query.

    ``branch_ids``: the branch(es) whose stock is summed (``None`` = every branch of the business).
    """
    n = normalize_barcode(barcode)
    info = LookupBarcodeInfo(
        input=barcode, normalized=n.canonical, valid=n.valid, format=n.format
    )
    candidates = list(match_candidates(barcode, lookup=True))
    row = (
        await db.execute(
            _LOOKUP_SQL,
            {"business_id": business_id, "candidates": candidates, "branches": branch_ids},
        )
    ).mappings().first()
    if row is None:
        # ``suggestion`` is the extension point for the future Master Catalogue / Open Food Facts.
        return ProductLookupResponse(found=False, barcode=info, product=None, suggestion=None)

    info.matched_barcode = row["matched_barcode"]
    product_id = row["product_id"]
    cost: Decimal | None = None
    if row["barcode_variation_id"] is not None:
        cost = await get_fallback_cost_per_unit(db, business_id, product_id, row["barcode_variation_id"])
    if not cost:
        cost = await get_fallback_cost_per_unit(db, business_id, product_id, None)
    product = LookupProduct(
        id=product_id,
        name=row["name"],
        variation_id=row["variation_id"],
        unit=LookupUnit(id=row["base_unit_id"], name=row["unit_name"], symbol=row["unit_symbol"]),
        sale_price=row["sale_price"],
        cost_price=cost if cost else None,
        stock_on_hand=row["stock_on_hand"],
        track_expiry=bool(row["track_expiry"]),
        image_url=row["image_url"],
    )
    return ProductLookupResponse(found=True, barcode=info, product=product, suggestion=None)


# ═════════════════════════════════════════════════════════════════════════════
# POST /products/quick-add
# ═════════════════════════════════════════════════════════════════════════════


class _LineError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _Stock:
    index: int
    product_id: UUID
    qty: Decimal
    cost: Decimal
    batch_no: str | None
    expiry: date | None


@dataclass
class _Hit:
    product_id: UUID
    variation_id: UUID | None
    deleted: bool


@dataclass
class _State:
    db: AsyncSession
    business_id: UUID
    user_id: UUID
    can_update_prices: bool
    require_batch: bool
    default_list_id: UUID
    categories: set[UUID] = field(default_factory=set)
    units: set[UUID] = field(default_factory=set)
    rates: set[UUID] = field(default_factory=set)
    brands: dict[str, UUID] = field(default_factory=dict)  # lower(name) -> id
    products: dict[UUID, Product] = field(default_factory=dict)
    hits: dict[str, _Hit] = field(default_factory=dict)  # stored barcode spelling -> hit
    default_unit_id: UUID | None = None


def _as_uuid(value: Any, code: str, label: str) -> UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value).strip())
    except ValueError:
        raise _LineError(code, f"{label} is not a valid id") from None


def _as_decimal(value: Any, code: str, label: str, *, quant: Decimal, maximum: Decimal) -> Decimal | None:
    """None/empty -> None; otherwise a finite Decimal >= 0 quantized to ``quant``."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise _LineError(code, f"{label} must be a number") from None
    if not number.is_finite():
        raise _LineError(code, f"{label} must be a number")
    if number < 0:
        raise _LineError(code, f"{label} cannot be negative")
    if number > maximum:
        raise _LineError(code, f"{label} is too large")
    return number.quantize(quant, rounding=ROUND_HALF_UP)


def _clean(value: str | None, limit: int, code: str, label: str) -> str | None:
    text_ = (value or "").strip()
    if not text_:
        return None
    if len(text_) > limit:
        raise _LineError(code, f"{label} is longer than {limit} characters")
    return text_


def _parse_expiry_value(value: Any) -> date | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return _parse_expiry(str(value))
    except ValueError as exc:
        raise _LineError("invalid_expiry_date", str(exc)) from None


async def _load_references(state: _State, lines: list[QuickAddLine]) -> None:
    """Read every category / unit / tax rate / brand / barcode / product the request touches, once."""
    db, biz = state.db, state.business_id

    def ids_of(attr: str) -> set[UUID]:
        out: set[UUID] = set()
        for ln in lines:
            try:
                got = _as_uuid(getattr(ln, attr), "x", attr)
            except _LineError:
                continue
            if got is not None:
                out.add(got)
        return out

    cat_ids, unit_ids, rate_ids = ids_of("category_id"), ids_of("unit_id"), ids_of("tax_rate_id")
    if cat_ids:
        state.categories = set(
            (
                await db.execute(
                    select(Category.id).where(
                        Category.business_id == biz, Category.deleted_at.is_(None), Category.id.in_(cat_ids)
                    )
                )
            ).scalars()
        )
    if unit_ids:
        state.units = set(
            (
                await db.execute(
                    select(Unit.id).where(
                        Unit.business_id == biz, Unit.deleted_at.is_(None), Unit.id.in_(unit_ids)
                    )
                )
            ).scalars()
        )
    if rate_ids:
        state.rates = set(
            (
                await db.execute(
                    select(TaxRate.id).where(
                        TaxRate.business_id == biz,
                        TaxRate.deleted_at.is_(None),
                        TaxRate.is_active.is_(True),
                        TaxRate.id.in_(rate_ids),
                    )
                )
            ).scalars()
        )
    brand_names = {(ln.brand or "").strip().lower() for ln in lines if (ln.brand or "").strip()}
    if brand_names:
        for b in (
            await db.execute(
                select(Brand.id, Brand.name).where(
                    Brand.business_id == biz,
                    Brand.deleted_at.is_(None),
                    func.lower(Brand.name).in_(brand_names),
                )
            )
        ).all():
            state.brands.setdefault(b.name.strip().lower(), b.id)

    candidates: set[str] = set()
    for ln in lines:
        candidates.update(match_candidates(ln.barcode))
    if candidates:
        # Soft-deleted rows are read too: the unique index on (business_id, barcode) covers them.
        for b in (
            await db.execute(
                select(Barcode.barcode, Barcode.product_id, Barcode.variation_id, Barcode.deleted_at).where(
                    Barcode.business_id == biz, Barcode.barcode.in_(candidates)
                )
            )
        ).all():
            cur = state.hits.get(b.barcode)
            if cur is None or (cur.deleted and b.deleted_at is None):
                state.hits[b.barcode] = _Hit(b.product_id, b.variation_id, b.deleted_at is not None)
        live_ids = {h.product_id for h in state.hits.values() if not h.deleted}
        if live_ids:
            state.products = {
                p.id: p
                for p in (
                    await db.execute(
                        select(Product).where(
                            Product.business_id == biz,
                            Product.id.in_(live_ids),
                            Product.deleted_at.is_(None),
                        )
                    )
                ).scalars()
            }


async def _default_unit(state: _State) -> UUID:
    """The business's "Piece" unit (created once when it does not exist yet)."""
    if state.default_unit_id is not None:
        return state.default_unit_id
    db, biz = state.db, state.business_id
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"default_unit:{biz}"}
    )
    found = (
        await db.execute(
            select(Unit.id)
            .where(
                Unit.business_id == biz,
                Unit.deleted_at.is_(None),
                func.lower(Unit.name) == DEFAULT_UNIT_NAME.lower(),
            )
            .order_by(Unit.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if found is None:
        now = datetime.now(timezone.utc)
        unit = Unit(
            business_id=biz,
            name=DEFAULT_UNIT_NAME,
            symbol=DEFAULT_UNIT_SYMBOL,
            is_base_unit=True,
            allows_decimal=False,
            created_by=state.user_id,
            created_at=now,
            updated_at=now,
        )
        db.add(unit)
        await db.flush()
        found = unit.id
    state.default_unit_id = found
    return found


def _register_hit(state: _State, spelling: str, product: Product, variation_id: UUID | None) -> None:
    state.hits[spelling] = _Hit(product.id, variation_id, False)
    state.products[product.id] = product


def _find_existing(state: _State, raw_barcode: str) -> tuple[Product | None, bool]:
    """(live product matching any spelling, a soft-deleted twin exists)."""
    deleted_twin = False
    for spelling in match_candidates(raw_barcode):
        hit = state.hits.get(spelling)
        if hit is None:
            continue
        product = state.products.get(hit.product_id) if not hit.deleted else None
        if product is not None:
            return product, False
        deleted_twin = True
    return None, deleted_twin


async def _process_line(
    state: _State,
    line: QuickAddLine,
    index: int,
    seen_in_request: set[str],
    stock: list[_Stock],
) -> QuickAddLineResult:
    """Validate + apply one line inside the caller's savepoint. Raises _LineError on bad input."""
    db, biz, actor = state.db, state.business_id, state.user_id

    # ── barcode
    raw = (line.barcode or "").strip()
    if not raw:
        raise _LineError("barcode_required", "barcode is required")
    n = normalize_barcode(raw)
    if n.empty and not n.custom:
        raise _LineError("invalid_barcode", "barcode has no digits")
    if not 4 <= len(n.canonical) <= 100:
        raise _LineError("invalid_barcode", "barcode must be 4-100 characters")

    # ── numbers
    qty = _as_decimal(line.qty, "invalid_qty", "qty", quant=_QTY_STEP, maximum=_MAX_QTY)
    if qty is None:
        raise _LineError("invalid_qty", "qty is required (use 0 to add the product without stock)")
    cost = _as_decimal(line.cost_price, "invalid_cost_price", "cost_price", quant=_CENT, maximum=_MAX_MONEY) or Decimal("0.00")
    sale = _as_decimal(line.sale_price, "invalid_sale_price", "sale_price", quant=_CENT, maximum=_MAX_MONEY) or Decimal("0.00")
    mrp = _as_decimal(line.mrp, "invalid_mrp", "mrp", quant=_CENT, maximum=_MAX_MONEY)
    batch_no = _clean(line.batch_no, 100, "invalid_batch_no", "batch_no")
    expiry = _parse_expiry_value(line.expiry_date)
    pack_size = _clean(line.pack_size, 50, "invalid_pack_size", "pack_size")

    existing, deleted_twin = _find_existing(state, raw)
    note: str | None = None

    if existing is None:
        if deleted_twin:
            raise _LineError(
                "barcode_deleted",
                "this barcode belongs to a deleted product; restore or rename that product first",
            )
        # ── new product
        name = (line.name or "").strip()
        if not name:
            raise _LineError("name_required", "name is required for a new product")
        if not 2 <= len(name) <= 255:
            raise _LineError("invalid_name", "name must be 2-255 characters")
        category_id = _as_uuid(line.category_id, "invalid_category", "category_id")
        if category_id is not None and category_id not in state.categories:
            raise _LineError("invalid_category", "category not found")
        unit_id = _as_uuid(line.unit_id, "invalid_unit", "unit_id")
        if unit_id is not None and unit_id not in state.units:
            raise _LineError("invalid_unit", "unit not found")
        rate_id = _as_uuid(line.tax_rate_id, "invalid_tax_rate", "tax_rate_id")
        if rate_id is not None and rate_id not in state.rates:
            raise _LineError("invalid_tax_rate", "tax rate not found or not active")
        track_expiry = bool(line.track_expiry)
        brand_text = _clean(line.brand, 255, "invalid_brand", "brand")
        image_url = _clean(line.image_url, 2000, "invalid_image_url", "image_url")
        if qty > 0:
            if track_expiry and expiry is None:
                raise _LineError("expiry_required", "expiry_date is required for a product that tracks expiry")
            if state.require_batch and not batch_no:
                raise _LineError("batch_required", "batch_no is required by this business's settings")

        brand_id: UUID | None = None
        new_brand: Brand | None = None
        if brand_text:
            brand_id = state.brands.get(brand_text.lower())
            if brand_id is None:
                now = datetime.now(timezone.utc)
                new_brand = Brand(
                    business_id=biz, name=brand_text, is_active=True,
                    created_by=actor, created_at=now, updated_at=now,
                )
                db.add(new_brand)
                await db.flush()
                brand_id = new_brand.id
        if unit_id is None:
            unit_id = await _default_unit(state)

        try:
            data = CreateProductRequest(
                name=name,
                category_id=category_id,
                brand_id=brand_id,
                base_unit_id=unit_id,
                product_type=ProductTypeEnum.standard,
                tracking_type=TrackingTypeEnum.none,
                is_tax_exempt=bool(line.is_tax_exempt),
                tax_rate_id=rate_id,  # null = the business default tax rate applies at sale time
                image_url=image_url,
                track_expiry=track_expiry,
                pack_size=pack_size,
                mrp=mrp,
                barcodes=[NewBarcodeInput(barcode=n.canonical, barcode_type=n.barcode_type, is_primary=True)],
            )
        except ValidationError as exc:
            first = exc.errors()[0]
            raise _LineError("invalid_line", f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}") from None

        product = await create_product_in_session(db, biz, data, actor)
        if sale > 0:  # a 0 price is "not priced yet": no price-list item, the product shows in needs_attention
            await upsert_price_item(db, biz, state.default_list_id, product.id, None, sale, Decimal("1"), actor)
        if new_brand is not None:
            state.brands[brand_text.lower()] = new_brand.id
        _register_hit(state, n.canonical, product, product.default_variation_id)
        seen_in_request.add(n.canonical)
        result = QuickAddLineResult(
            client_line_id=line.client_line_id, status="created",
            product_id=product.id, variation_id=product.default_variation_id,
        )
    else:
        # ── existing product: add stock; touch prices only on request
        track_expiry = bool(existing.track_expiry)
        if qty > 0:
            if track_expiry and expiry is None:
                raise _LineError("expiry_required", "expiry_date is required for a product that tracks expiry")
            if state.require_batch and not batch_no:
                raise _LineError("batch_required", "batch_no is required by this business's settings")
        if line.update_existing_prices:
            if not state.can_update_prices:
                raise _LineError("forbidden", "updating prices of existing products needs the products.update permission")
            if sale > 0:
                await upsert_price_item(db, biz, state.default_list_id, existing.id, None, sale, Decimal("1"), actor)
            changed = False
            if mrp is not None and existing.mrp != mrp:
                existing.mrp, changed = mrp, True
            if pack_size is not None and existing.pack_size != pack_size:
                existing.pack_size, changed = pack_size, True
            if changed:
                existing.updated_by = actor
                existing.updated_at = datetime.now(timezone.utc)
                await db.flush()
        if n.canonical in seen_in_request:
            note = "duplicate_barcode_merged"
        seen_in_request.add(n.canonical)
        hit = next((state.hits[c] for c in match_candidates(raw) if c in state.hits and not state.hits[c].deleted), None)
        result = QuickAddLineResult(
            client_line_id=line.client_line_id, status="updated",
            product_id=existing.id,
            variation_id=await _default_variation_id(db, biz, existing.id, hit.variation_id if hit else None),
            note=note,
        )
        product = existing

    if qty > 0:
        stock.append(_Stock(index, product.id, qty, cost, batch_no, expiry))
    return result


async def _default_variation_id(db: AsyncSession, biz: UUID, product_id: UUID, barcode_variation: UUID | None) -> UUID | None:
    if barcode_variation is not None:
        return barcode_variation
    row = (
        await db.execute(
            text(
                "SELECT id FROM product_variations WHERE business_id = :b AND product_id = :p "
                "AND deleted_at IS NULL ORDER BY is_default DESC, created_at LIMIT 1"
            ),
            {"b": biz, "p": product_id},
        )
    ).first()
    return row[0] if row else None


def _error_result(line: QuickAddLine, code: str, message: str) -> QuickAddLineResult:
    return QuickAddLineResult(
        client_line_id=line.client_line_id, status="error", error=QuickAddLineError(code=code, message=message)
    )


async def _post_stock_adjustment(
    state: _State, branch_id: UUID, stock: list[_Stock], quick_add_id: UUID
) -> UUID:
    adj = await create_stock_adjustment(
        state.db,
        state.business_id,
        CreateStockAdjustmentRequest(
            branch_id=branch_id,
            reason=AdjustmentReasonEnum.opening_balance,
            notes=f"Quick Add {quick_add_id}",
            lines=[
                CreateAdjustmentLineRequest(
                    product_id=s.product_id, qty_delta=s.qty, cost_per_unit=s.cost,
                    batch_number=s.batch_no, expiry_date=s.expiry,
                )
                for s in stock
            ],
        ),
        state.user_id,
    )
    return adj.id


async def _post_purchase(
    state: _State, branch_id: UUID, supplier_id: UUID, stock: list[_Stock], quick_add_id: UUID
) -> tuple[UUID, UUID]:
    """One purchase order + one goods receipt for every stock line. Returns (po_id, receipt_id)."""
    notes = f"Quick Add {quick_add_id}"
    po = await create_purchase_order(
        state.db,
        state.business_id,
        CreatePurchaseOrderRequest(
            supplier_id=supplier_id,
            branch_id=branch_id,
            notes=notes,
            lines=[
                CreatePurchaseLineRequest(
                    product_id=s.product_id, ordered_qty=s.qty, cost_per_unit=s.cost,
                    batch_number=s.batch_no, expiry_date=s.expiry,
                )
                for s in stock
            ],
        ),
        state.user_id,
    )
    # Pair every receipt line with its PO line (several lines may share a product).
    pool: dict[tuple, list[UUID]] = {}
    for pl in po.lines:
        key = (pl.product_id, Decimal(pl.ordered_qty), Decimal(pl.cost_per_unit), pl.batch_number, pl.expiry_date)
        pool.setdefault(key, []).append(pl.id)
    receipt_lines = []
    for s in stock:
        key = (s.product_id, s.qty, s.cost, s.batch_no, s.expiry)
        ids = pool.get(key) or next(
            (v for k, v in pool.items() if k[0] == s.product_id and v and Decimal(k[1]) == s.qty), []
        )
        receipt_lines.append(
            CreateReceiptLineRequest(
                product_id=s.product_id, purchase_line_id=ids.pop(0) if ids else None,
                qty_received=s.qty, cost_per_unit=s.cost, batch_number=s.batch_no, expiry_date=s.expiry,
            )
        )
    try:
        receipt = await create_purchase_receipt(
            state.db,
            state.business_id,
            CreatePurchaseReceiptRequest(
                branch_id=branch_id, supplier_id=supplier_id, purchase_order_id=po.id,
                notes=notes, lines=receipt_lines,
            ),
            state.user_id,
        )
    except Exception:
        # Do not leave a dangling draft PO behind.
        try:
            await update_purchase_order_status(state.db, po.id, state.business_id, "cancelled", state.user_id)
        except Exception:  # pragma: no cover - best effort
            log.exception("quick-add: could not cancel purchase order %s", po.id)
        raise
    return po.id, receipt.id


async def run_quick_add(
    db: AsyncSession,
    business_id: UUID,
    user_id: UUID,
    body: QuickAddRequest,
    *,
    branch_id: UUID,
    can_update_prices: bool,
) -> QuickAddResponse:
    quick_add_id = uuid.uuid4()
    purchase_mode = body.mode == "purchase" and body.supplier_id is not None

    # Whole-request checks (nothing written yet): branch and supplier must be this business's.
    branch = (
        await db.execute(
            select(Branch.id).where(
                Branch.id == branch_id, Branch.business_id == business_id, Branch.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if branch is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Branch not found")
    if body.supplier_id is not None:
        await get_supplier_by_id(db, body.supplier_id, business_id)

    cfg = await quick_add_settings.load_settings(db, business_id)
    state = _State(
        db=db, business_id=business_id, user_id=user_id, can_update_prices=can_update_prices,
        require_batch=bool(cfg[quick_add_settings.KEY_REQUIRE_BATCH]),
        default_list_id=await ensure_default_price_list(db, business_id, user_id),
    )
    await _load_references(state, body.lines)

    results: list[QuickAddLineResult] = []
    stock: list[_Stock] = []
    seen: set[str] = set()
    created_ids: list[str] = []
    for index, line in enumerate(body.lines):
        stock_mark = len(stock)
        try:
            async with db.begin_nested():  # this line's own savepoint
                result = await _process_line(state, line, index, seen, stock)
        except _LineError as exc:
            del stock[stock_mark:]
            results.append(_error_result(line, exc.code, exc.message))
            continue
        except HTTPException as exc:
            del stock[stock_mark:]
            code = "conflict" if exc.status_code == status.HTTP_409_CONFLICT else "invalid_line"
            results.append(_error_result(line, code, str(exc.detail)))
            continue
        except IntegrityError:
            del stock[stock_mark:]
            results.append(_error_result(line, "conflict", "barcode or SKU already exists"))
            continue
        except Exception:  # never let one line take the request down
            del stock[stock_mark:]
            log.exception("quick-add: unexpected error on line %s", line.client_line_id)
            results.append(_error_result(line, "internal_error", "unexpected error while saving this line"))
            continue
        if result.status == "created":
            created_ids.append(str(result.product_id))
        results.append(result)

    # Phase 1 done: products / prices / unit / brands are durable.
    await db.commit()

    # Phase 2: one stock document for every line with qty > 0.
    purchase_id = receipt_id = adjustment_id = None
    if stock:
        try:
            if purchase_mode:
                purchase_id, receipt_id = await _post_purchase(state, branch_id, body.supplier_id, stock, quick_add_id)
            else:
                adjustment_id = await _post_stock_adjustment(state, branch_id, stock, quick_add_id)
        except HTTPException as exc:
            await db.rollback()
            _fail_stock_lines(results, stock, str(exc.detail))
        except Exception:
            await db.rollback()
            log.exception("quick-add: stock posting failed")
            _fail_stock_lines(results, stock, "stock could not be posted")

    created = sum(1 for r in results if r.status == "created")
    updated = sum(1 for r in results if r.status == "updated")
    failed = sum(1 for r in results if r.status == "error")

    await log_audit(
        db,
        business_id=business_id,
        user_id=user_id,
        branch_id=branch_id,
        action="create",
        table_name="products",
        record_id=quick_add_id,
        old_values=None,
        new_values={
            "event": "quick_add",
            "mode": "purchase" if purchase_mode else "opening_stock",
            "supplier_id": str(body.supplier_id) if body.supplier_id else None,
            "branch_id": str(branch_id),
            "lines": len(body.lines),
            "created": created,
            "updated": updated,
            "failed": failed,
            "stock_lines": len(stock),
            "purchase_id": str(purchase_id) if purchase_id else None,
            "receipt_id": str(receipt_id) if receipt_id else None,
            "adjustment_id": str(adjustment_id) if adjustment_id else None,
            "created_product_ids": created_ids[:500],
            "actor": str(user_id),
        },
    )
    await db.commit()

    return QuickAddResponse(
        created=created, updated=updated, failed=failed,
        purchase_id=purchase_id, receipt_id=receipt_id, adjustment_id=adjustment_id,
        quick_add_id=quick_add_id, lines=results,
    )


def _fail_stock_lines(results: list[QuickAddLineResult], stock: list[_Stock], message: str) -> None:
    """Phase 2 failed: the product stays, the line reports that its stock was not posted."""
    # results are in request order, one per line; _Stock.index is the line index.
    for s in stock:
        r = results[s.index]
        r.status = "error"
        r.error = QuickAddLineError(code="stock_failed", message=f"product saved but stock not posted: {message}")
