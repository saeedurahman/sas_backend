"""Dataset exports (CSV / XLSX) behind ``GET /export/{dataset}``.

Rules every dataset follows, so files from different screens look alike:

* Column names are fixed ``snake_case`` per dataset (see ``DATASETS``).
* Dates are ``YYYY-MM-DD`` and timestamps ``YYYY-MM-DD HH:MM`` in the business timezone
  (default Asia/Karachi); money is a plain number with two decimals (no symbol, no grouping),
  quantities up to four decimals.
* Text cells that could start a spreadsheet formula (= + - @ tab CR LF) are prefixed with
  ``'`` (``sanitize_cell``), in CSV and XLSX alike.
* Row cap ``EXPORT_MAX_ROWS``: larger results are refused up front with 413.
* SQL-backed datasets stream from a server-side cursor in batches, so memory stays flat.
"""

from __future__ import annotations

import csv
import io
import re
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.database import AsyncSessionLocal
from app.services import analytics_service as analytics
from app.services.business_time import local_day_bounds_utc

_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r", "\n")
_PLAIN_NUMBER = re.compile(r"^[+-]?\d+(\.\d+)?$")
_CENT = Decimal("0.01")
_QTY = Decimal("0.0001")
_BATCH = 500


def sanitize_cell(value: Any) -> Any:
    """Neutralise spreadsheet formula injection in user-controlled text.

    A string that starts with = + - @ (or tab/CR/LF) gets a leading single quote so Excel,
    Sheets and LibreOffice show it as text. Plain numbers such as "-5.00" or a phone number
    "+923001234567" are left alone.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_TRIGGERS):
        if not _PLAIN_NUMBER.match(value):
            return "'" + value
    return value


@dataclass(frozen=True)
class Col:
    key: str
    kind: str = "text"  # text | date | datetime | money | qty | int | pct

    @property
    def header(self) -> str:
        return self.key


@dataclass
class ExportCtx:
    business_id: UUID
    zone: ZoneInfo
    date_from: date
    date_to: date
    branch_id: UUID | None
    start_utc: datetime = field(init=False)
    end_utc: datetime = field(init=False)

    def __post_init__(self) -> None:
        self.start_utc = local_day_bounds_utc(self.date_from, self.zone)[0]
        self.end_utc = local_day_bounds_utc(self.date_to, self.zone)[1]

    def params(self, **extra: Any) -> dict[str, Any]:
        out: dict[str, Any] = {
            "business_id": self.business_id,
            "start": self.start_utc,
            "end": self.end_utc,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "branch_id": self.branch_id,
            "tz": self.zone.key,
        }
        out.update(extra)
        return out


@dataclass(frozen=True)
class Dataset:
    name: str
    columns: tuple[Col, ...]
    description: str
    uses_dates: bool = False
    uses_branch: bool = False
    extra_permission: str | None = None  # on top of reports.export
    sql: Callable[[ExportCtx], str] | None = None
    loader: Callable[[AsyncSession, ExportCtx], Awaitable[list[dict[str, Any]]]] | None = None


# ── value formatting ─────────────────────────────────────────────────────────


def _dec(value: Any) -> Decimal:
    return Decimal("0") if value is None else Decimal(str(value))


def _stamp(value: Any, zone: ZoneInfo, with_time: bool) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = value.astimezone(zone)
        return value.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")
    return value.strftime("%Y-%m-%d")


def _qty_text(value: Decimal) -> str:
    text_ = f"{value.quantize(_QTY, rounding=ROUND_HALF_UP):f}"
    if "." in text_:
        text_ = text_.rstrip("0").rstrip(".")
    return text_ or "0"


def format_cell(value: Any, col: Col, zone: ZoneInfo, *, numeric: bool) -> Any:
    """One output cell. ``numeric`` keeps numbers as numbers (XLSX); CSV gets strings."""
    if value is None:
        return "" if col.kind in ("text", "date", "datetime") or not numeric else None
    if col.kind == "date":
        return _stamp(value, zone, False)
    if col.kind == "datetime":
        return _stamp(value, zone, True)
    if col.kind in ("money", "pct"):
        number = _dec(value).quantize(_CENT, rounding=ROUND_HALF_UP)
        return number if numeric else f"{number:.2f}"
    if col.kind == "qty":
        number = _dec(value).quantize(_QTY, rounding=ROUND_HALF_UP).normalize()
        return (number if number == number.to_integral() else number) if numeric else _qty_text(_dec(value))
    if col.kind == "int":
        return int(value) if numeric else str(int(value))
    return sanitize_cell("" if value is None else str(value))


def _row_cells(row: dict[str, Any], dataset: Dataset, zone: ZoneInfo, numeric: bool) -> list[Any]:
    return [format_cell(row.get(c.key), c, zone, numeric=numeric) for c in dataset.columns]


# ── SQL datasets ─────────────────────────────────────────────────────────────


def _branch_clause(ctx: ExportCtx, column: str) -> str:
    return f" AND {column} = :branch_id" if ctx.branch_id is not None else ""


def _sales_sql(ctx: ExportCtx) -> str:
    return f"""
        SELECT s.sale_number, COALESCE(s.client_receipt_no, '') AS client_receipt_no,
               s.sold_at AS sold_at, b.name AS branch, COALESCE(c.name, '') AS customer,
               s.sale_type::text AS sale_type, s.status::text AS status,
               COALESCE(l.subtotal, 0) AS subtotal, COALESCE(l.discount, 0) AS discount,
               COALESCE(l.tax, 0) AS tax,
               COALESCE(l.subtotal, 0) - COALESCE(l.discount, 0) + COALESCE(l.tax, 0) AS total,
               COALESCE(p.paid, 0) AS paid,
               GREATEST(COALESCE(l.subtotal, 0) - COALESCE(l.discount, 0) + COALESCE(l.tax, 0)
                        - COALESCE(p.paid, 0), 0) AS balance_due,
               COALESCE(p.methods, '') AS payment_methods,
               COALESCE(u.full_name, '') AS cashier
        FROM sales s
        JOIN branches b ON b.id = s.branch_id
        LEFT JOIN customers c ON c.id = s.customer_id
        LEFT JOIN users u ON u.id = s.created_by
        LEFT JOIN LATERAL (
            SELECT SUM(sl.qty * sl.unit_price) AS subtotal, SUM(sl.discount_amount) AS discount,
                   SUM(sl.tax_amount) AS tax
            FROM sale_lines sl WHERE sl.sale_id = s.id AND sl.deleted_at IS NULL
        ) l ON TRUE
        LEFT JOIN LATERAL (
            SELECT SUM(sp.amount) AS paid, STRING_AGG(DISTINCT sp.payment_method::text, ', ') AS methods
            FROM sale_payments sp
            WHERE sp.sale_id = s.id AND sp.deleted_at IS NULL AND sp.status = 'completed'
        ) p ON TRUE
        WHERE s.business_id = :business_id AND s.deleted_at IS NULL
          AND s.sold_at >= :start AND s.sold_at < :end{_branch_clause(ctx, "s.branch_id")}
        ORDER BY s.sold_at, s.id
    """


def _sale_lines_sql(ctx: ExportCtx) -> str:
    return f"""
        SELECT s.sale_number, s.sold_at AS sold_at, b.name AS branch, s.status::text AS status,
               p.name AS product, COALESCE(v.name, '') AS variation,
               COALESCE(v.sku, p.sku, '') AS sku,
               sl.qty AS qty, sl.unit_price AS unit_price, sl.discount_amount AS discount,
               sl.tax_rate AS tax_rate, sl.tax_amount AS tax,
               sl.qty * sl.unit_price - sl.discount_amount + sl.tax_amount AS line_total,
               sl.cost_per_unit AS cost_per_unit
        FROM sale_lines sl
        JOIN sales s ON s.id = sl.sale_id
        JOIN branches b ON b.id = s.branch_id
        JOIN products p ON p.id = sl.product_id
        LEFT JOIN product_variations v ON v.id = sl.variation_id
        WHERE sl.business_id = :business_id AND sl.deleted_at IS NULL AND s.deleted_at IS NULL
          AND s.sold_at >= :start AND s.sold_at < :end{_branch_clause(ctx, "s.branch_id")}
        ORDER BY s.sold_at, s.id, sl.line_order, sl.id
    """


def _products_sql(ctx: ExportCtx) -> str:
    branch = _branch_clause(ctx, "sv.branch_id")
    return f"""
        SELECT p.name AS name, COALESCE(p.sku, '') AS sku,
               COALESCE(bc.barcode, '') AS barcode, COALESCE(cat.name, '') AS category,
               COALESCE(u.symbol, '') AS unit,
               cost.cost AS cost, price.unit_price AS price,
               COALESCE(stock.qty, 0) AS stock
        FROM products p
        LEFT JOIN categories cat ON cat.id = p.category_id
        LEFT JOIN units u ON u.id = p.base_unit_id
        LEFT JOIN LATERAL (
            SELECT b.barcode FROM barcodes b
            WHERE b.product_id = p.id AND b.deleted_at IS NULL
            ORDER BY b.is_primary DESC, b.created_at LIMIT 1
        ) bc ON TRUE
        LEFT JOIN LATERAL (
            SELECT COALESCE(
                (SELECT SUM(pl.qty_remaining * pl.cost_per_unit) / NULLIF(SUM(pl.qty_remaining), 0)
                   FROM purchase_lines pl
                  WHERE pl.product_id = p.id AND pl.deleted_at IS NULL AND pl.qty_remaining > 0),
                (SELECT pl.cost_per_unit FROM purchase_lines pl
                  WHERE pl.product_id = p.id AND pl.deleted_at IS NULL
                  ORDER BY pl.created_at DESC LIMIT 1)
            ) AS cost
        ) cost ON TRUE
        LEFT JOIN LATERAL (
            SELECT pli.unit_price FROM price_list_items pli
            JOIN price_lists pls ON pls.id = pli.price_list_id
            WHERE pli.product_id = p.id AND pli.variation_id IS NULL AND pli.min_qty = 1
              AND pli.deleted_at IS NULL AND pls.is_default AND pls.deleted_at IS NULL
            LIMIT 1
        ) price ON TRUE
        LEFT JOIN LATERAL (
            SELECT SUM(sv.qty) AS qty FROM stock_levels sv
            WHERE sv.product_id = p.id AND sv.deleted_at IS NULL{branch}
        ) stock ON TRUE
        WHERE p.business_id = :business_id AND p.deleted_at IS NULL
        ORDER BY p.name, p.id
    """


def _customers_sql(ctx: ExportCtx) -> str:
    return """
        SELECT c.name AS name, COALESCE(c.phone, '') AS phone, COALESCE(c.email, '') AS email,
               COALESCE(c.tax_id, '') AS tax_id, COALESCE(c.address_line1, '') AS address,
               COALESCE(c.city, '') AS city, c.credit_limit AS credit_limit,
               COALESCE(led.balance, 0) AS balance, COALESCE(pl.name, '') AS price_list,
               c.is_active::text AS is_active
        FROM customers c
        LEFT JOIN price_lists pl ON pl.id = c.price_list_id
        LEFT JOIN LATERAL (
            SELECT SUM(cl.amount) AS balance FROM customer_ledger cl
            WHERE cl.customer_id = c.id AND cl.deleted_at IS NULL
        ) led ON TRUE
        WHERE c.business_id = :business_id AND c.deleted_at IS NULL
        ORDER BY c.name, c.id
    """


def _customer_ledger_sql(ctx: ExportCtx) -> str:
    return """
        SELECT cl.entry_at AS entry_at, c.name AS customer, cl.entry_type::text AS entry_type,
               cl.amount AS amount, COALESCE(cl.reference_type::text, '') AS reference_type,
               COALESCE(s.sale_number, '') AS reference, COALESCE(cl.notes, '') AS notes
        FROM customer_ledger cl
        JOIN customers c ON c.id = cl.customer_id
        LEFT JOIN sales s ON s.id = cl.reference_id AND cl.reference_type::text = 'sale'
        WHERE cl.business_id = :business_id AND cl.deleted_at IS NULL
          AND cl.entry_at >= :start AND cl.entry_at < :end
        ORDER BY cl.entry_at, cl.id
    """


def _suppliers_sql(ctx: ExportCtx) -> str:
    return """
        SELECT s.name AS name, COALESCE(s.contact_person, '') AS contact_person,
               COALESCE(s.phone, '') AS phone, COALESCE(s.email, '') AS email,
               COALESCE(s.tax_id, '') AS tax_id, COALESCE(s.address_line1, '') AS address,
               COALESCE(s.city, '') AS city, COALESCE(s.payment_terms_days, 0) AS payment_terms_days,
               COALESCE(led.balance, 0) AS balance, s.is_active::text AS is_active
        FROM suppliers s
        LEFT JOIN LATERAL (
            SELECT SUM(sl.amount) AS balance FROM supplier_ledger sl
            WHERE sl.supplier_id = s.id AND sl.deleted_at IS NULL
        ) led ON TRUE
        WHERE s.business_id = :business_id AND s.deleted_at IS NULL
        ORDER BY s.name, s.id
    """


def _supplier_ledger_sql(ctx: ExportCtx) -> str:
    return """
        SELECT sl.entry_at AS entry_at, s.name AS supplier, sl.entry_type::text AS entry_type,
               sl.amount AS amount, COALESCE(sl.reference_type::text, '') AS reference_type,
               COALESCE(sl.notes, '') AS notes
        FROM supplier_ledger sl
        JOIN suppliers s ON s.id = sl.supplier_id
        WHERE sl.business_id = :business_id AND sl.deleted_at IS NULL
          AND sl.entry_at >= :start AND sl.entry_at < :end
        ORDER BY sl.entry_at, sl.id
    """


def _expenses_sql(ctx: ExportCtx) -> str:
    return f"""
        SELECT e.expense_number AS expense_number, e.expense_date AS expense_date,
               b.name AS branch, cat.name AS category, COALESCE(sup.name, '') AS supplier,
               COALESCE(e.description, '') AS description, e.amount AS amount,
               e.tax_amount AS tax, e.amount + e.tax_amount AS total
        FROM expenses e
        JOIN branches b ON b.id = e.branch_id
        JOIN expense_categories cat ON cat.id = e.expense_category_id
        LEFT JOIN suppliers sup ON sup.id = e.supplier_id
        WHERE e.business_id = :business_id AND e.deleted_at IS NULL
          AND e.expense_date BETWEEN :date_from AND :date_to{_branch_clause(ctx, "e.branch_id")}
        ORDER BY e.expense_date, e.expense_number, e.id
    """


def _purchases_sql(ctx: ExportCtx) -> str:
    return f"""
        SELECT po.po_number AS po_number, po.ordered_at AS ordered_at, b.name AS branch,
               COALESCE(sup.name, '') AS supplier, po.status::text AS status,
               COALESCE(l.lines, 0) AS lines, COALESCE(l.ordered_total, 0) AS ordered_total,
               COALESCE(l.received_total, 0) AS received_total, COALESCE(po.notes, '') AS notes
        FROM purchase_orders po
        JOIN branches b ON b.id = po.branch_id
        LEFT JOIN suppliers sup ON sup.id = po.supplier_id
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS lines, SUM(pl.ordered_qty * pl.cost_per_unit) AS ordered_total,
                   SUM(pl.received_qty * pl.cost_per_unit) AS received_total
            FROM purchase_lines pl WHERE pl.purchase_order_id = po.id AND pl.deleted_at IS NULL
        ) l ON TRUE
        WHERE po.business_id = :business_id AND po.deleted_at IS NULL
          AND po.ordered_at >= :start AND po.ordered_at < :end{_branch_clause(ctx, "po.branch_id")}
        ORDER BY po.ordered_at, po.id
    """


# ── analytics-backed datasets (small, computed) ─────────────────────────────


async def _load_inventory(db: AsyncSession, ctx: ExportCtx) -> list[dict[str, Any]]:
    items = await analytics.get_stock_valuation(db, ctx.business_id, ctx.branch_id)
    return [
        {
            "product": i.product_name,
            "variation": i.variation_name or "",
            "branch": i.branch_name,
            "qty": i.current_qty,
            "avg_cost": i.avg_cost,
            "total_value": i.total_value,
        }
        for i in items
    ]


async def _load_profit_loss(db: AsyncSession, ctx: ExportCtx) -> list[dict[str, Any]]:
    pl = await analytics.get_profit_loss(db, ctx.business_id, ctx.date_from, ctx.date_to, ctx.branch_id)
    rows = [
        {"section": "summary", "line": "revenue", "amount": pl.total_revenue},
        {"section": "summary", "line": "cost_of_goods_sold", "amount": pl.total_cogs},
        {"section": "summary", "line": "gross_profit", "amount": pl.gross_profit},
        {"section": "summary", "line": "gross_margin_pct", "amount": pl.gross_margin_pct},
        {"section": "summary", "line": "expenses", "amount": pl.total_expenses},
        {"section": "summary", "line": "net_profit", "amount": pl.net_profit},
        {"section": "summary", "line": "net_margin_pct", "amount": pl.net_margin_pct},
    ]
    rows += [
        {"section": "expense_breakdown", "line": e.category_name, "amount": e.total_amount}
        for e in pl.expense_breakdown
    ]
    return rows


async def _load_tax_summary(db: AsyncSession, ctx: ExportCtx) -> list[dict[str, Any]]:
    ts = await analytics.get_tax_summary(db, ctx.business_id, ctx.date_from, ctx.date_to, ctx.branch_id)
    rows: list[dict[str, Any]] = [
        {
            "section": "sales_by_rate",
            "tax_rate": r.tax_rate,
            "taxable_amount": r.total_sales_amount,
            "tax_amount": r.total_tax_amount,
            "transactions": r.transaction_count,
        }
        for r in ts.tax_by_rate
    ]
    rows += [
        {"section": "total_sales", "tax_rate": None, "taxable_amount": ts.total_taxable_sales,
         "tax_amount": ts.total_tax_collected, "transactions": None},
        {"section": "total_purchases", "tax_rate": None, "taxable_amount": ts.total_taxable_purchases,
         "tax_amount": ts.total_tax_paid, "transactions": None},
    ]
    return rows


async def _load_cashiers(db: AsyncSession, ctx: ExportCtx) -> list[dict[str, Any]]:
    items = await analytics.get_cashier_performance(
        db, ctx.business_id, ctx.date_from, ctx.date_to, ctx.branch_id
    )
    return [
        {
            "rank": i.rank,
            "cashier": i.user_name,
            "sales": i.total_sales,
            "revenue": i.total_revenue,
            "returns": i.total_returns,
            "voids": i.total_voids,
            "discounts": i.total_discounts,
            "avg_order_value": i.avg_order_value,
        }
        for i in items
    ]


async def _load_top_products(db: AsyncSession, ctx: ExportCtx) -> list[dict[str, Any]]:
    items = await analytics.get_top_products(
        db, ctx.business_id, ctx.date_from, ctx.date_to, ctx.branch_id, limit=1000
    )
    return [
        {
            "rank": i.rank,
            "product": i.product_name,
            "variation": i.variation_name or "",
            "qty_sold": i.total_qty_sold,
            "revenue": i.total_revenue,
            "cost": i.total_cost,
            "gross_profit": i.gross_profit,
        }
        for i in items
    ]


def _cols(spec: str) -> tuple[Col, ...]:
    """``"name sku qty:qty"`` -> columns; a bare key is text, ``key:kind`` sets the kind."""
    out = []
    for part in spec.split():
        key, _, kind = part.partition(":")
        out.append(Col(key, kind or "text"))
    return tuple(out)


DATASETS: dict[str, Dataset] = {
    d.name: d
    for d in (
        Dataset(
            "sales",
            _cols(
                "sale_number client_receipt_no sold_at:datetime branch customer sale_type status "
                "subtotal:money discount:money tax:money total:money paid:money balance_due:money "
                "payment_methods cashier"
            ),
            "One row per sale in the date range",
            uses_dates=True, uses_branch=True, sql=_sales_sql,
        ),
        Dataset(
            "sale_lines",
            _cols(
                "sale_number sold_at:datetime branch status product variation sku qty:qty "
                "unit_price:money discount:money tax_rate:pct tax:money line_total:money "
                "cost_per_unit:money"
            ),
            "One row per sale line in the date range",
            uses_dates=True, uses_branch=True, sql=_sale_lines_sql,
        ),
        Dataset(
            "inventory",
            _cols("product variation branch qty:qty avg_cost:money total_value:money"),
            "Stock valuation per product and branch, as of now",
            uses_branch=True, extra_permission="inventory.view", loader=_load_inventory,
        ),
        Dataset(
            "products",
            _cols("name sku barcode category unit cost:money price:money stock:qty"),
            "Product catalog; the same columns are accepted by POST /products/import "
            "(stock = current stock, import column opening_stock)",
            uses_branch=True, sql=_products_sql,
        ),
        Dataset(
            "customers",
            _cols(
                "name phone email tax_id address city credit_limit:money balance:money "
                "price_list is_active"
            ),
            "Customers with ledger balance (negative = customer owes)",
            extra_permission="customers.view", sql=_customers_sql,
        ),
        Dataset(
            "customer_ledger",
            _cols("entry_at:datetime customer entry_type amount:money reference_type reference notes"),
            "Customer ledger entries in the date range",
            uses_dates=True, extra_permission="customers.ledger.view", sql=_customer_ledger_sql,
        ),
        Dataset(
            "suppliers",
            _cols(
                "name contact_person phone email tax_id address city payment_terms_days:int "
                "balance:money is_active"
            ),
            "Suppliers with ledger balance",
            extra_permission="suppliers.view", sql=_suppliers_sql,
        ),
        Dataset(
            "supplier_ledger",
            _cols("entry_at:datetime supplier entry_type amount:money reference_type notes"),
            "Supplier ledger entries in the date range",
            uses_dates=True, extra_permission="suppliers.ledger.view", sql=_supplier_ledger_sql,
        ),
        Dataset(
            "expenses",
            _cols(
                "expense_number expense_date:date branch category supplier description "
                "amount:money tax:money total:money"
            ),
            "Expenses dated in the range",
            uses_dates=True, uses_branch=True, extra_permission="expenses.view", sql=_expenses_sql,
        ),
        Dataset(
            "purchases",
            _cols(
                "po_number ordered_at:datetime branch supplier status lines:int "
                "ordered_total:money received_total:money notes"
            ),
            "Purchase orders ordered in the range",
            uses_dates=True, uses_branch=True, extra_permission="inventory.purchase_orders.view",
            sql=_purchases_sql,
        ),
        Dataset(
            "profit_loss",
            _cols("section line amount:money"),
            "Profit and loss summary with expense breakdown",
            uses_dates=True, uses_branch=True, extra_permission="reports.financial",
            loader=_load_profit_loss,
        ),
        Dataset(
            "tax_summary",
            _cols("section tax_rate:pct taxable_amount:money tax_amount:money transactions:int"),
            "Tax collected by rate, plus purchase tax paid",
            uses_dates=True, uses_branch=True, extra_permission="reports.financial",
            loader=_load_tax_summary,
        ),
        Dataset(
            "cashier_performance",
            _cols(
                "rank:int cashier sales:int revenue:money returns:int voids:int "
                "discounts:money avg_order_value:money"
            ),
            "Sales, returns and voids per cashier",
            uses_dates=True, uses_branch=True, loader=_load_cashiers,
        ),
        Dataset(
            "top_products",
            _cols(
                "rank:int product variation qty_sold:qty revenue:money cost:money gross_profit:money"
            ),
            "Best-selling products by revenue (top 1000)",
            uses_dates=True, uses_branch=True, loader=_load_top_products,
        ),
    )
}
ALIASES = {"stock_valuation": "inventory", "stock": "inventory"}


def resolve_dataset(name: str) -> Dataset:
    dataset = DATASETS.get(ALIASES.get(name, name))
    if dataset is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Unknown dataset '{name}'. Available: {', '.join(sorted(DATASETS))}",
        )
    return dataset


# ── execution ────────────────────────────────────────────────────────────────


def too_large(limit: int) -> HTTPException:
    return HTTPException(
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        f"Export too large: more than {limit} rows. Narrow the date range or pick a branch "
        "and try again.",
    )


@dataclass
class Prepared:
    dataset: Dataset
    ctx: ExportCtx
    sql: str | None = None
    rows: list[dict[str, Any]] | None = None
    row_count: int = 0


async def prepare(db: AsyncSession, dataset: Dataset, ctx: ExportCtx) -> Prepared:
    """Run the cheap checks (row cap) before any byte is sent, so a 413 is a clean response."""
    limit = settings.export_max_rows
    if dataset.loader is not None:
        rows = await dataset.loader(db, ctx)
        if len(rows) > limit:
            raise too_large(limit)
        return Prepared(dataset, ctx, rows=rows, row_count=len(rows))
    assert dataset.sql is not None
    sql = dataset.sql(ctx)
    count = (
        await db.execute(text(f"SELECT COUNT(*) FROM ({sql}) AS export_rows"), _bind(sql, ctx))  # noqa: S608
    ).scalar_one()
    if count > limit:
        raise too_large(limit)
    return Prepared(dataset, ctx, sql=sql, row_count=int(count))


def _bind(sql: str, ctx: ExportCtx) -> dict[str, Any]:
    """Only the parameters the statement mentions (asyncpg rejects unused ones)."""
    wanted = set(re.findall(r":([a-z_]+)", sql))
    return {k: v for k, v in ctx.params().items() if k in wanted}


async def iter_rows(prepared: Prepared) -> AsyncIterator[dict[str, Any]]:
    if prepared.rows is not None:
        for row in prepared.rows:
            yield row
        return
    assert prepared.sql is not None
    # Own session: the request's session may be closed by the time the body streams.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT set_config('TimeZone', :tz, true)"), {"tz": prepared.ctx.zone.key}
        )
        result = await session.stream(
            text(prepared.sql),
            _bind(prepared.sql, prepared.ctx),
            execution_options={"yield_per": 1000},
        )
        async for row in result.mappings():
            yield dict(row)


async def stream_csv(prepared: Prepared) -> AsyncIterator[bytes]:
    dataset, zone = prepared.dataset, prepared.ctx.zone
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([c.header for c in dataset.columns])
    # UTF-8 BOM so Excel opens non-Latin names (Urdu) correctly; importers strip it.
    yield b"\xef\xbb\xbf" + buffer.getvalue().encode()
    buffer.seek(0)
    buffer.truncate()
    pending = 0
    async for row in iter_rows(prepared):
        writer.writerow(_row_cells(row, dataset, zone, numeric=False))
        pending += 1
        if pending >= _BATCH:
            yield buffer.getvalue().encode()
            buffer.seek(0)
            buffer.truncate()
            pending = 0
    if pending:
        yield buffer.getvalue().encode()


_WIDTH = {"text": 24, "date": 12, "datetime": 17, "money": 14, "qty": 12, "int": 10, "pct": 10}


async def stream_xlsx(prepared: Prepared) -> AsyncIterator[bytes]:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    dataset, zone = prepared.dataset, prepared.ctx.zone
    wb = Workbook(write_only=True)
    ws = wb.create_sheet(dataset.name[:31])
    for index, col in enumerate(dataset.columns, start=1):
        ws.column_dimensions[get_column_letter(index)].width = max(
            _WIDTH.get(col.kind, 16), len(col.header) + 2
        )
    ws.freeze_panes = "A2"
    bold = Font(bold=True)
    header_cells = []
    for col in dataset.columns:
        cell = WriteOnlyCell(ws, value=col.header)
        cell.font = bold
        header_cells.append(cell)
    ws.append(header_cells)

    async for row in iter_rows(prepared):
        cells: list[Any] = []
        for col, value in zip(
            dataset.columns, _row_cells(row, dataset, zone, numeric=True), strict=True
        ):
            if col.kind in ("money", "pct") and value is not None and value != "":
                cell = WriteOnlyCell(ws, value=value)
                cell.number_format = "0.00"
                cells.append(cell)
            else:
                cells.append(value)
        ws.append(cells)

    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as spool:
        wb.save(spool)
        spool.seek(0)
        while chunk := spool.read(64 * 1024):
            yield chunk
