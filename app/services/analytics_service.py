"""Analytics and reporting services — SQL aggregation only."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.analytics import (
    BranchComparisonItem,
    CashInDrawerResponse,
    CashierPerformanceItem,
    CategoryPerformanceItem,
    CustomerInsightItem,
    DashboardSummaryResponse,
    DeadStockItem,
    EnhancedDashboardResponse,
    ExpenseSummaryItem,
    FraudAlertItem,
    InventoryInsightsResponse,
    LowStockAlertItem,
    PaymentBreakdownItem,
    PeakDayItem,
    PeakHourItem,
    ProductMovementItem,
    ProfitLossResponse,
    RecentTransactionItem,
    SalesSummaryResponse,
    SalesTrendItem,
    StockValuationItem,
    TaxByRateItem,
    TaxSummaryResponse,
    TodayVsYesterdayResponse,
    TopProductItem,
)

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")
_SALE_ACTIVE = "s.status NOT IN ('cancelled', 'voided')"
_DAY_NAMES = [
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
]


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _dec(value) -> Decimal:
    if value is None:
        return _ZERO
    return Decimal(str(value))


def _margin_pct(profit: Decimal, revenue: Decimal) -> Decimal:
    if revenue <= 0:
        return _ZERO
    return (profit / revenue * _HUNDRED).quantize(Decimal("0.01"))


def _branch_clause(branch_id: UUID | None, alias: str = "s") -> str:
    if branch_id is None:
        return ""
    return f" AND {alias}.branch_id = :branch_id"


def _line_revenue_expr(alias: str = "sl") -> str:
    return f"({alias}.qty * {alias}.unit_price) - {alias}.discount_amount"


def _line_cost_expr(alias: str = "sl") -> str:
    return f"{alias}.qty * {alias}.cost_per_unit"


async def _scalar(db: AsyncSession, sql: str, params: dict) -> Decimal:
    result = await db.execute(text(sql), params)
    return _dec(result.scalar_one())


async def _scalar_int(db: AsyncSession, sql: str, params: dict) -> int:
    result = await db.execute(text(sql), params)
    val = result.scalar_one()
    return int(val or 0)


def _base_sale_joins() -> str:
    return """
        FROM sale_lines sl
        INNER JOIN sales s ON s.id = sl.sale_id
            AND s.business_id = sl.business_id
        WHERE sl.business_id = :business_id
            AND sl.deleted_at IS NULL
            AND s.deleted_at IS NULL
            AND {_SALE_ACTIVE}
    """.format(_SALE_ACTIVE=_SALE_ACTIVE)


async def get_dashboard_summary(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
) -> DashboardSummaryResponse:
    today = _today()
    branch_sql = _branch_clause(branch_id, "s")
    params: dict = {"business_id": business_id, "today": today}

    if branch_id is not None:
        params["branch_id"] = branch_id

    base_from = _base_sale_joins() + branch_sql + " AND s.sold_at::date = :today"

    today_revenue_sql = f"""
        SELECT COALESCE(SUM({_line_revenue_expr()}), 0)
        {base_from}
    """
    today_cost_sql = f"""
        SELECT COALESCE(SUM({_line_cost_expr()}), 0)
        {base_from}
    """
    today_tx_sql = f"""
        SELECT COUNT(DISTINCT s.id)
        {base_from}
    """

    month_branch = _branch_clause(branch_id, "s")
    month_from = (
        _base_sale_joins()
        + month_branch
        + " AND s.sold_at >= DATE_TRUNC('month', CURRENT_DATE)"
    )
    month_revenue_sql = f"""
        SELECT COALESCE(SUM({_line_revenue_expr()}), 0)
        {month_from}
    """
    month_cost_sql = f"""
        SELECT COALESCE(SUM({_line_cost_expr()}), 0)
        {month_from}
    """

    low_stock_sql = """
        SELECT COUNT(*)
        FROM product_locations pl
        INNER JOIN branches b ON b.id = pl.branch_id
        LEFT JOIN LATERAL (
            SELECT COALESCE(SUM(sm.qty), 0) AS current_qty
            FROM stock_movements sm
            WHERE sm.business_id = pl.business_id
                AND sm.branch_id = pl.branch_id
                AND sm.product_id = pl.product_id
                AND sm.variation_id IS NOT DISTINCT FROM pl.variation_id
                AND sm.deleted_at IS NULL
        ) stock ON TRUE
        WHERE pl.business_id = :business_id
            AND pl.deleted_at IS NULL
            AND pl.min_stock_level IS NOT NULL
            AND stock.current_qty < pl.min_stock_level
    """
    if branch_id is not None:
        low_stock_sql += " AND pl.branch_id = :branch_id"

    open_shifts_sql = """
        SELECT COUNT(*)
        FROM register_shifts rs
        WHERE rs.business_id = :business_id
            AND rs.status = 'open'
            AND rs.deleted_at IS NULL
    """
    if branch_id is not None:
        open_shifts_sql += " AND rs.branch_id = :branch_id"

    customer_bal_sql = """
        SELECT COALESCE(SUM(ABS(sub.balance)), 0)
        FROM (
            SELECT SUM(cl.amount) AS balance
            FROM customer_ledger cl
            WHERE cl.business_id = :business_id
                AND cl.deleted_at IS NULL
            GROUP BY cl.customer_id
            HAVING SUM(cl.amount) < 0
        ) sub
    """
    supplier_bal_sql = """
        SELECT COALESCE(SUM(ABS(sub.balance)), 0)
        FROM (
            SELECT SUM(sl.amount) AS balance
            FROM supplier_ledger sl
            WHERE sl.business_id = :business_id
                AND sl.deleted_at IS NULL
            GROUP BY sl.supplier_id
            HAVING SUM(sl.amount) < 0
        ) sub
    """

    (
        today_revenue,
        today_cost,
        today_transactions,
        month_revenue,
        month_cost,
        low_stock_alerts,
        open_shifts,
        pending_customer,
        pending_supplier,
    ) = await asyncio.gather(
        _scalar(db, today_revenue_sql, params),
        _scalar(db, today_cost_sql, params),
        _scalar_int(db, today_tx_sql, params),
        _scalar(db, month_revenue_sql, params),
        _scalar(db, month_cost_sql, params),
        _scalar_int(db, low_stock_sql, params),
        _scalar_int(db, open_shifts_sql, params),
        _scalar(db, customer_bal_sql, params),
        _scalar(db, supplier_bal_sql, params),
    )

    today_profit = today_revenue - today_cost
    month_profit = month_revenue - month_cost
    today_avg = (
        today_revenue / today_transactions
        if today_transactions > 0
        else _ZERO
    )

    return DashboardSummaryResponse(
        today_revenue=today_revenue,
        today_transactions=today_transactions,
        today_profit=today_profit,
        today_avg_order=today_avg.quantize(Decimal("0.01")),
        this_month_revenue=month_revenue,
        this_month_profit=month_profit,
        low_stock_alerts=low_stock_alerts,
        open_shifts=open_shifts,
        pending_customer_balances=pending_customer,
        pending_supplier_balances=pending_supplier,
    )


async def get_sales_summary(
    db: AsyncSession,
    business_id: UUID,
    date_from: date,
    date_to: date,
    branch_id: UUID | None = None,
) -> SalesSummaryResponse:
    branch_sql = _branch_clause(branch_id, "s")
    params: dict = {
        "business_id": business_id,
        "date_from": date_from,
        "date_to": date_to,
    }
    if branch_id is not None:
        params["branch_id"] = branch_id

    summary_sql = f"""
        SELECT
            COALESCE(SUM({_line_revenue_expr()}), 0) AS total_revenue,
            COALESCE(SUM({_line_cost_expr()}), 0) AS total_cost,
            COUNT(DISTINCT s.id) AS total_transactions,
            COALESCE(SUM(sl.discount_amount), 0) AS total_discount,
            COALESCE(SUM(sl.tax_amount), 0) AS total_tax
        {_base_sale_joins()}
            {branch_sql}
            AND s.sold_at::date BETWEEN :date_from AND :date_to
    """
    result = await db.execute(text(summary_sql), params)
    row = result.one()

    returns_sql = f"""
        SELECT COALESCE(SUM(srl.qty * srl.unit_price + srl.tax_amount), 0)
        FROM sale_return_lines srl
        INNER JOIN sale_returns sr ON sr.id = srl.sale_return_id
            AND sr.business_id = srl.business_id
        WHERE srl.business_id = :business_id
            AND srl.deleted_at IS NULL
            AND sr.deleted_at IS NULL
            AND sr.returned_at::date BETWEEN :date_from AND :date_to
            {_branch_clause(branch_id, "sr").replace("s.", "sr.")}
    """
    total_returns = await _scalar(db, returns_sql, params)

    total_revenue = _dec(row.total_revenue)
    total_cost = _dec(row.total_cost)
    total_transactions = int(row.total_transactions or 0)
    gross_profit = total_revenue - total_cost
    avg_order = (
        total_revenue / total_transactions if total_transactions > 0 else _ZERO
    )

    return SalesSummaryResponse(
        period_start=date_from,
        period_end=date_to,
        total_revenue=total_revenue,
        total_cost=total_cost,
        gross_profit=gross_profit,
        gross_margin_pct=_margin_pct(gross_profit, total_revenue),
        total_transactions=total_transactions,
        avg_order_value=avg_order.quantize(Decimal("0.01")),
        total_discount=_dec(row.total_discount),
        total_tax=_dec(row.total_tax),
        total_returns=total_returns,
        net_revenue=total_revenue - total_returns,
    )


async def get_sales_trend(
    db: AsyncSession,
    business_id: UUID,
    period: str,
    date_from: date,
    date_to: date,
    branch_id: UUID | None = None,
) -> list[SalesTrendItem]:
    trunc_map = {"daily": "day", "weekly": "week", "monthly": "month"}
    trunc = trunc_map.get(period, "day")
    format_map = {
        "day": "YYYY-MM-DD",
        "week": "YYYY-MM-DD",
        "month": "YYYY-MM",
    }
    fmt = format_map[trunc]
    branch_sql = _branch_clause(branch_id, "s")
    params: dict = {
        "business_id": business_id,
        "date_from": date_from,
        "date_to": date_to,
    }
    if branch_id is not None:
        params["branch_id"] = branch_id

    sql = f"""
        SELECT
            TO_CHAR(DATE_TRUNC('{trunc}', s.sold_at), '{fmt}') AS period,
            COALESCE(SUM({_line_revenue_expr()}), 0) AS revenue,
            COUNT(DISTINCT s.id) AS transactions
        {_base_sale_joins()}
            {branch_sql}
            AND s.sold_at::date BETWEEN :date_from AND :date_to
        GROUP BY DATE_TRUNC('{trunc}', s.sold_at)
        ORDER BY DATE_TRUNC('{trunc}', s.sold_at) ASC
    """
    result = await db.execute(text(sql), params)
    items: list[SalesTrendItem] = []
    for row in result:
        revenue = _dec(row.revenue)
        tx_count = int(row.transactions or 0)
        avg = revenue / tx_count if tx_count > 0 else _ZERO
        items.append(
            SalesTrendItem(
                period=row.period,
                revenue=revenue,
                transactions=tx_count,
                avg_order_value=avg.quantize(Decimal("0.01")),
            )
        )
    return items


async def get_payment_breakdown(
    db: AsyncSession,
    business_id: UUID,
    date_from: date,
    date_to: date,
    branch_id: UUID | None = None,
) -> list[PaymentBreakdownItem]:
    branch_sql = _branch_clause(branch_id, "s")
    params: dict = {
        "business_id": business_id,
        "date_from": date_from,
        "date_to": date_to,
    }
    if branch_id is not None:
        params["branch_id"] = branch_id

    total_sql = f"""
        SELECT COALESCE(SUM(sp.amount), 0)
        FROM sale_payments sp
        INNER JOIN sales s ON s.id = sp.sale_id
            AND s.business_id = sp.business_id
        WHERE sp.business_id = :business_id
            AND sp.deleted_at IS NULL
            AND s.deleted_at IS NULL
            AND sp.status = 'completed'
            AND {_SALE_ACTIVE}
            {branch_sql}
            AND s.sold_at::date BETWEEN :date_from AND :date_to
    """
    grand_total = await _scalar(db, total_sql, params)

    breakdown_sql = f"""
        SELECT
            sp.payment_method,
            COALESCE(SUM(sp.amount), 0) AS total_amount,
            COUNT(DISTINCT sp.sale_id) AS transaction_count
        FROM sale_payments sp
        INNER JOIN sales s ON s.id = sp.sale_id
            AND s.business_id = sp.business_id
        WHERE sp.business_id = :business_id
            AND sp.deleted_at IS NULL
            AND s.deleted_at IS NULL
            AND sp.status = 'completed'
            AND {_SALE_ACTIVE}
            {branch_sql}
            AND s.sold_at::date BETWEEN :date_from AND :date_to
        GROUP BY sp.payment_method
        ORDER BY total_amount DESC
    """
    result = await db.execute(text(breakdown_sql), params)
    items: list[PaymentBreakdownItem] = []
    for row in result:
        amount = _dec(row.total_amount)
        pct = (
            (amount / grand_total * _HUNDRED).quantize(Decimal("0.01"))
            if grand_total > 0
            else _ZERO
        )
        items.append(
            PaymentBreakdownItem(
                payment_method=row.payment_method,
                total_amount=amount,
                transaction_count=int(row.transaction_count or 0),
                percentage=pct,
            )
        )
    return items


async def get_top_products(
    db: AsyncSession,
    business_id: UUID,
    date_from: date,
    date_to: date,
    branch_id: UUID | None = None,
    limit: int = 20,
) -> list[TopProductItem]:
    branch_sql = _branch_clause(branch_id, "s")
    params: dict = {
        "business_id": business_id,
        "date_from": date_from,
        "date_to": date_to,
        "limit": limit,
    }
    if branch_id is not None:
        params["branch_id"] = branch_id

    sql = f"""
        WITH product_stats AS (
            SELECT
                sl.product_id,
                sl.variation_id,
                p.name AS product_name,
                pv.name AS variation_name,
                COALESCE(SUM(sl.qty), 0) AS total_qty_sold,
                COALESCE(SUM({_line_revenue_expr()}), 0) AS total_revenue,
                COALESCE(SUM({_line_cost_expr()}), 0) AS total_cost
            FROM sale_lines sl
            INNER JOIN sales s ON s.id = sl.sale_id
                AND s.business_id = sl.business_id
            INNER JOIN products p ON p.id = sl.product_id
            LEFT JOIN product_variations pv ON pv.id = sl.variation_id
            WHERE sl.business_id = :business_id
                AND sl.deleted_at IS NULL
                AND s.deleted_at IS NULL
                AND {_SALE_ACTIVE}
                {branch_sql}
                AND s.sold_at::date BETWEEN :date_from AND :date_to
            GROUP BY sl.product_id, sl.variation_id, p.name, pv.name
        ),
        ranked AS (
            SELECT
                *,
                total_revenue - total_cost AS gross_profit,
                DENSE_RANK() OVER (ORDER BY total_revenue DESC) AS rank
            FROM product_stats
        )
        SELECT *
        FROM ranked
        WHERE rank <= :limit
        ORDER BY rank ASC, total_revenue DESC
    """
    result = await db.execute(text(sql), params)
    return [
        TopProductItem(
            product_id=row.product_id,
            product_name=row.product_name,
            variation_name=row.variation_name,
            total_qty_sold=_dec(row.total_qty_sold),
            total_revenue=_dec(row.total_revenue),
            total_cost=_dec(row.total_cost),
            gross_profit=_dec(row.gross_profit),
            rank=int(row.rank),
        )
        for row in result
    ]


async def get_category_performance(
    db: AsyncSession,
    business_id: UUID,
    date_from: date,
    date_to: date,
    branch_id: UUID | None = None,
) -> list[CategoryPerformanceItem]:
    branch_sql = _branch_clause(branch_id, "s")
    params: dict = {
        "business_id": business_id,
        "date_from": date_from,
        "date_to": date_to,
    }
    if branch_id is not None:
        params["branch_id"] = branch_id

    sql = f"""
        WITH category_stats AS (
            SELECT
                c.id AS category_id,
                c.name AS category_name,
                COALESCE(SUM({_line_revenue_expr()}), 0) AS total_revenue,
                COUNT(DISTINCT s.id) AS total_transactions
            {_base_sale_joins()}
            INNER JOIN products p ON p.id = sl.product_id
            INNER JOIN categories c ON c.id = p.category_id
                {branch_sql}
                AND s.sold_at::date BETWEEN :date_from AND :date_to
            GROUP BY c.id, c.name
        ),
        grand AS (
            SELECT COALESCE(SUM(total_revenue), 0) AS total FROM category_stats
        )
        SELECT
            cs.category_id,
            cs.category_name,
            cs.total_revenue,
            cs.total_transactions,
            CASE
                WHEN g.total > 0
                THEN ROUND(cs.total_revenue / g.total * 100, 2)
                ELSE 0
            END AS percentage_of_total
        FROM category_stats cs
        CROSS JOIN grand g
        ORDER BY cs.total_revenue DESC
    """
    result = await db.execute(text(sql), params)
    return [
        CategoryPerformanceItem(
            category_id=row.category_id,
            category_name=row.category_name,
            total_revenue=_dec(row.total_revenue),
            total_transactions=int(row.total_transactions or 0),
            percentage_of_total=_dec(row.percentage_of_total),
        )
        for row in result
    ]


async def get_branch_comparison(
    db: AsyncSession,
    business_id: UUID,
    date_from: date,
    date_to: date,
) -> list[BranchComparisonItem]:
    params = {
        "business_id": business_id,
        "date_from": date_from,
        "date_to": date_to,
    }
    sql = f"""
        SELECT
            s.branch_id,
            b.name AS branch_name,
            COALESCE(SUM({_line_revenue_expr()}), 0) AS total_revenue,
            COUNT(DISTINCT s.id) AS total_transactions,
            COALESCE(SUM({_line_revenue_expr()} - {_line_cost_expr()}), 0) AS total_profit
        {_base_sale_joins()}
        INNER JOIN branches b ON b.id = s.branch_id
            AND s.sold_at::date BETWEEN :date_from AND :date_to
        GROUP BY s.branch_id, b.name
        ORDER BY total_revenue DESC
    """
    result = await db.execute(text(sql), params)
    items: list[BranchComparisonItem] = []
    for row in result:
        revenue = _dec(row.total_revenue)
        tx = int(row.total_transactions or 0)
        avg = revenue / tx if tx > 0 else _ZERO
        items.append(
            BranchComparisonItem(
                branch_id=row.branch_id,
                branch_name=row.branch_name,
                total_revenue=revenue,
                total_transactions=tx,
                avg_order_value=avg.quantize(Decimal("0.01")),
                total_profit=_dec(row.total_profit),
            )
        )
    return items


async def get_cashier_performance(
    db: AsyncSession,
    business_id: UUID,
    date_from: date,
    date_to: date,
    branch_id: UUID | None = None,
) -> list[CashierPerformanceItem]:
    branch_sql = _branch_clause(branch_id, "s")
    branch_ret = _branch_clause(branch_id, "sr").replace("s.", "sr.")
    params: dict = {
        "business_id": business_id,
        "date_from": date_from,
        "date_to": date_to,
    }
    if branch_id is not None:
        params["branch_id"] = branch_id

    sql = f"""
        WITH sales_stats AS (
            SELECT
                s.created_by AS user_id,
                u.full_name AS user_name,
                COUNT(DISTINCT s.id) FILTER (
                    WHERE {_SALE_ACTIVE}
                ) AS total_sales,
                COALESCE(SUM({_line_revenue_expr()}) FILTER (
                    WHERE {_SALE_ACTIVE}
                ), 0) AS total_revenue,
                COUNT(DISTINCT s.id) FILTER (
                    WHERE s.status IN ('cancelled', 'voided')
                ) AS total_voids,
                COALESCE(SUM(sl.discount_amount) FILTER (
                    WHERE {_SALE_ACTIVE}
                ), 0) AS total_discounts
            {_base_sale_joins()}
            INNER JOIN users u ON u.id = s.created_by
                {branch_sql}
                AND s.sold_at::date BETWEEN :date_from AND :date_to
            GROUP BY s.created_by, u.full_name
        ),
        return_stats AS (
            SELECT sr.created_by AS user_id, COUNT(*) AS total_returns
            FROM sale_returns sr
            WHERE sr.business_id = :business_id
                AND sr.deleted_at IS NULL
                AND sr.returned_at::date BETWEEN :date_from AND :date_to
                {branch_ret}
            GROUP BY sr.created_by
        ),
        combined AS (
            SELECT
                ss.user_id,
                ss.user_name,
                ss.total_sales,
                ss.total_revenue,
                COALESCE(rs.total_returns, 0) AS total_returns,
                ss.total_voids,
                ss.total_discounts,
                DENSE_RANK() OVER (ORDER BY ss.total_revenue DESC) AS rank
            FROM sales_stats ss
            LEFT JOIN return_stats rs ON rs.user_id = ss.user_id
        )
        SELECT * FROM combined
        ORDER BY rank ASC, total_revenue DESC
    """
    result = await db.execute(text(sql), params)
    items: list[CashierPerformanceItem] = []
    for row in result:
        revenue = _dec(row.total_revenue)
        sales_count = int(row.total_sales or 0)
        avg = revenue / sales_count if sales_count > 0 else _ZERO
        items.append(
            CashierPerformanceItem(
                user_id=row.user_id,
                user_name=row.user_name,
                total_sales=sales_count,
                total_revenue=revenue,
                total_returns=int(row.total_returns or 0),
                total_voids=int(row.total_voids or 0),
                total_discounts=_dec(row.total_discounts),
                avg_order_value=avg.quantize(Decimal("0.01")),
                rank=int(row.rank),
            )
        )
    return items


async def get_fraud_alerts(
    db: AsyncSession,
    business_id: UUID,
    date_from: date,
    date_to: date,
    branch_id: UUID | None = None,
) -> list[FraudAlertItem]:
    branch_sql = _branch_clause(branch_id, "s")
    branch_ret = _branch_clause(branch_id, "sr").replace("s.", "sr.")
    params: dict = {
        "business_id": business_id,
        "date_from": date_from,
        "date_to": date_to,
    }
    if branch_id is not None:
        params["branch_id"] = branch_id

    alerts: list[FraudAlertItem] = []

    high_voids_sql = f"""
        SELECT
            s.created_by AS user_id,
            u.full_name AS user_name,
            s.branch_id,
            s.sold_at::date AS sale_date,
            COUNT(*) AS void_count
        FROM sales s
        INNER JOIN users u ON u.id = s.created_by
        WHERE s.business_id = :business_id
            AND s.deleted_at IS NULL
            AND s.status IN ('cancelled', 'voided')
            AND s.sold_at::date BETWEEN :date_from AND :date_to
            {branch_sql}
        GROUP BY s.created_by, u.full_name, s.branch_id, s.sold_at::date
        HAVING COUNT(*) > 3
    """
    void_result = await db.execute(text(high_voids_sql), params)
    for row in void_result:
        count = int(row.void_count)
        severity = "high" if count > 6 else "medium" if count > 4 else "low"
        alerts.append(
            FraudAlertItem(
                alert_type="high_voids",
                user_id=row.user_id,
                user_name=row.user_name,
                branch_id=row.branch_id,
                count=count,
                total_amount=_ZERO,
                severity=severity,
                description=(
                    f"{count} voided/cancelled sales on {row.sale_date}"
                ),
            )
        )

    excessive_sql = f"""
        WITH cashier_avg AS (
            SELECT
                s.created_by AS user_id,
                u.full_name AS user_name,
                s.branch_id,
                AVG(sl.discount_pct) AS avg_discount
            {_base_sale_joins()}
            INNER JOIN users u ON u.id = s.created_by
                {branch_sql}
                AND s.sold_at::date BETWEEN :date_from AND :date_to
            GROUP BY s.created_by, u.full_name, s.branch_id
        ),
        business_avg AS (
            SELECT AVG(sl.discount_pct) AS avg_discount
            {_base_sale_joins()}
                {branch_sql}
                AND s.sold_at::date BETWEEN :date_from AND :date_to
        )
        SELECT
            ca.user_id,
            ca.user_name,
            ca.branch_id,
            ca.avg_discount,
            ba.avg_discount AS business_avg
        FROM cashier_avg ca
        CROSS JOIN business_avg ba
        WHERE ba.avg_discount > 0
            AND ca.avg_discount > ba.avg_discount * 2
    """
    disc_result = await db.execute(text(excessive_sql), params)
    for row in disc_result:
        avg_disc = _dec(row.avg_discount)
        biz_avg = _dec(row.business_avg)
        ratio = avg_disc / biz_avg if biz_avg > 0 else _ZERO
        severity = "high" if ratio > 3 else "medium"
        alerts.append(
            FraudAlertItem(
                alert_type="excessive_discounts",
                user_id=row.user_id,
                user_name=row.user_name,
                branch_id=row.branch_id,
                count=1,
                total_amount=avg_disc,
                severity=severity,
                description=(
                    f"Average discount {avg_disc}% vs business avg {biz_avg}%"
                ),
            )
        )

    unusual_returns_sql = f"""
        WITH sales_count AS (
            SELECT s.created_by AS user_id, COUNT(*) AS sale_count
            FROM sales s
            WHERE s.business_id = :business_id
                AND s.deleted_at IS NULL
                AND {_SALE_ACTIVE}
                AND s.sold_at::date BETWEEN :date_from AND :date_to
                {branch_sql}
            GROUP BY s.created_by
        ),
        return_count AS (
            SELECT sr.created_by AS user_id, COUNT(*) AS return_count
            FROM sale_returns sr
            WHERE sr.business_id = :business_id
                AND sr.deleted_at IS NULL
                AND sr.returned_at::date BETWEEN :date_from AND :date_to
                {branch_ret}
            GROUP BY sr.created_by
        )
        SELECT
            sc.user_id,
            u.full_name AS user_name,
            COALESCE(s.branch_id, sr_branch.branch_id) AS branch_id,
            rc.return_count,
            sc.sale_count
        FROM sales_count sc
        INNER JOIN return_count rc ON rc.user_id = sc.user_id
        INNER JOIN users u ON u.id = sc.user_id
        LEFT JOIN LATERAL (
            SELECT s.branch_id FROM sales s
            WHERE s.created_by = sc.user_id
                AND s.business_id = :business_id
                AND s.deleted_at IS NULL
            LIMIT 1
        ) s ON TRUE
        LEFT JOIN LATERAL (
            SELECT sr.branch_id FROM sale_returns sr
            WHERE sr.created_by = sc.user_id
                AND sr.business_id = :business_id
                AND sr.deleted_at IS NULL
            LIMIT 1
        ) sr_branch ON TRUE
        WHERE sc.sale_count > 0
            AND (rc.return_count::numeric / sc.sale_count) > 0.2
    """
    ret_result = await db.execute(text(unusual_returns_sql), params)
    for row in ret_result:
        ret_count = int(row.return_count)
        sale_count = int(row.sale_count)
        rate = (Decimal(ret_count) / Decimal(sale_count) * _HUNDRED).quantize(
            Decimal("0.1")
        )
        alerts.append(
            FraudAlertItem(
                alert_type="unusual_returns",
                user_id=row.user_id,
                user_name=row.user_name,
                branch_id=row.branch_id,
                count=ret_count,
                total_amount=_ZERO,
                severity="high" if rate > 40 else "medium",
                description=(
                    f"Return rate {rate}% ({ret_count}/{sale_count} transactions)"
                ),
            )
        )

    drawer_sql = """
        SELECT
            rs.opened_by AS user_id,
            u.full_name AS user_name,
            rs.branch_id,
            COUNT(rt.id) AS cash_out_count,
            COALESCE(SUM(rt.amount), 0) AS total_amount
        FROM register_shifts rs
        INNER JOIN register_transactions rt ON rt.register_shift_id = rs.id
            AND rt.business_id = rs.business_id
        INNER JOIN users u ON u.id = rs.opened_by
        WHERE rs.business_id = :business_id
            AND rs.deleted_at IS NULL
            AND rt.deleted_at IS NULL
            AND rt.tx_type = 'cash_out'
            AND rs.opened_at::date BETWEEN :date_from AND :date_to
    """
    if branch_id is not None:
        drawer_sql += " AND rs.branch_id = :branch_id"
    drawer_sql += """
        AND NOT EXISTS (
            SELECT 1 FROM register_transactions sale_tx
            WHERE sale_tx.register_shift_id = rs.id
                AND sale_tx.business_id = rs.business_id
                AND sale_tx.deleted_at IS NULL
                AND sale_tx.tx_type = 'sale'
        )
        GROUP BY rs.opened_by, u.full_name, rs.branch_id
    """
    drawer_result = await db.execute(text(drawer_sql), params)
    for row in drawer_result:
        alerts.append(
            FraudAlertItem(
                alert_type="no_sale_drawer",
                user_id=row.user_id,
                user_name=row.user_name,
                branch_id=row.branch_id,
                count=int(row.cash_out_count),
                total_amount=_dec(row.total_amount),
                severity="high",
                description="Cash drawer activity without any sales on shift",
            )
        )

    severity_order = {"high": 0, "medium": 1, "low": 2}
    alerts.sort(key=lambda a: (severity_order.get(a.severity, 9), -a.count))
    return alerts


async def get_stock_valuation(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
) -> list[StockValuationItem]:
    params: dict = {"business_id": business_id}
    branch_filter = ""
    if branch_id is not None:
        params["branch_id"] = branch_id
        branch_filter = " AND sm.branch_id = :branch_id"

    sql = f"""
        WITH stock_qty AS (
            SELECT
                sm.branch_id,
                sm.product_id,
                sm.variation_id,
                COALESCE(SUM(sm.qty), 0) AS current_qty
            FROM stock_movements sm
            WHERE sm.business_id = :business_id
                AND sm.deleted_at IS NULL
                {branch_filter}
            GROUP BY sm.branch_id, sm.product_id, sm.variation_id
            HAVING COALESCE(SUM(sm.qty), 0) > 0
        ),
        avg_costs AS (
            SELECT
                pl.product_id,
                pl.variation_id,
                AVG(pl.cost_per_unit) AS avg_cost
            FROM purchase_lines pl
            WHERE pl.business_id = :business_id
                AND pl.deleted_at IS NULL
                AND pl.qty_remaining > 0
            GROUP BY pl.product_id, pl.variation_id
        )
        SELECT
            sq.product_id,
            p.name AS product_name,
            sq.variation_id,
            pv.name AS variation_name,
            sq.branch_id,
            b.name AS branch_name,
            sq.current_qty,
            COALESCE(ac.avg_cost, 0) AS avg_cost,
            sq.current_qty * COALESCE(ac.avg_cost, 0) AS total_value
        FROM stock_qty sq
        INNER JOIN products p ON p.id = sq.product_id
        INNER JOIN branches b ON b.id = sq.branch_id
        LEFT JOIN product_variations pv ON pv.id = sq.variation_id
        LEFT JOIN avg_costs ac ON ac.product_id = sq.product_id
            AND ac.variation_id IS NOT DISTINCT FROM sq.variation_id
        ORDER BY total_value DESC
    """
    result = await db.execute(text(sql), params)
    return [
        StockValuationItem(
            product_id=row.product_id,
            product_name=row.product_name,
            variation_id=row.variation_id,
            variation_name=row.variation_name,
            branch_id=row.branch_id,
            branch_name=row.branch_name,
            current_qty=_dec(row.current_qty),
            avg_cost=_dec(row.avg_cost),
            total_value=_dec(row.total_value),
        )
        for row in result
    ]


async def get_inventory_insights(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
) -> InventoryInsightsResponse:
    params: dict = {"business_id": business_id}
    branch_pl = ""
    branch_sm = ""
    if branch_id is not None:
        params["branch_id"] = branch_id
        branch_pl = " AND pl.branch_id = :branch_id"
        branch_sm = " AND sm.branch_id = :branch_id"

    total_products_sql = f"""
        SELECT COUNT(DISTINCT (pl.product_id, pl.variation_id))
        FROM product_locations pl
        WHERE pl.business_id = :business_id
            AND pl.deleted_at IS NULL
            {branch_pl}
    """
    stock_value_sql = f"""
        WITH stock_qty AS (
            SELECT
                sm.branch_id,
                sm.product_id,
                sm.variation_id,
                COALESCE(SUM(sm.qty), 0) AS current_qty
            FROM stock_movements sm
            WHERE sm.business_id = :business_id
                AND sm.deleted_at IS NULL
                {branch_sm}
            GROUP BY sm.branch_id, sm.product_id, sm.variation_id
        ),
        avg_costs AS (
            SELECT
                pl.product_id,
                pl.variation_id,
                AVG(pl.cost_per_unit) AS avg_cost
            FROM purchase_lines pl
            WHERE pl.business_id = :business_id
                AND pl.deleted_at IS NULL
                AND pl.qty_remaining > 0
            GROUP BY pl.product_id, pl.variation_id
        )
        SELECT COALESCE(SUM(sq.current_qty * COALESCE(ac.avg_cost, 0)), 0)
        FROM stock_qty sq
        LEFT JOIN avg_costs ac ON ac.product_id = sq.product_id
            AND ac.variation_id IS NOT DISTINCT FROM sq.variation_id
    """
    low_stock_sql = f"""
        SELECT
            pl.product_id,
            p.name AS product_name,
            pl.variation_id,
            pl.branch_id,
            b.name AS branch_name,
            COALESCE(stock.current_qty, 0) AS current_qty,
            pl.min_stock_level,
            pl.min_stock_level - COALESCE(stock.current_qty, 0) AS shortage
        FROM product_locations pl
        INNER JOIN products p ON p.id = pl.product_id
        INNER JOIN branches b ON b.id = pl.branch_id
        LEFT JOIN LATERAL (
            SELECT COALESCE(SUM(sm.qty), 0) AS current_qty
            FROM stock_movements sm
            WHERE sm.business_id = pl.business_id
                AND sm.branch_id = pl.branch_id
                AND sm.product_id = pl.product_id
                AND sm.variation_id IS NOT DISTINCT FROM pl.variation_id
                AND sm.deleted_at IS NULL
        ) stock ON TRUE
        WHERE pl.business_id = :business_id
            AND pl.deleted_at IS NULL
            AND pl.min_stock_level IS NOT NULL
            AND COALESCE(stock.current_qty, 0) < pl.min_stock_level
            {branch_pl}
        ORDER BY shortage DESC
        LIMIT 50
    """
    out_of_stock_sql = f"""
        SELECT COUNT(*)
        FROM product_locations pl
        LEFT JOIN LATERAL (
            SELECT COALESCE(SUM(sm.qty), 0) AS current_qty
            FROM stock_movements sm
            WHERE sm.business_id = pl.business_id
                AND sm.branch_id = pl.branch_id
                AND sm.product_id = pl.product_id
                AND sm.variation_id IS NOT DISTINCT FROM pl.variation_id
                AND sm.deleted_at IS NULL
        ) stock ON TRUE
        WHERE pl.business_id = :business_id
            AND pl.deleted_at IS NULL
            AND COALESCE(stock.current_qty, 0) <= 0
            {branch_pl}
    """

    total_products, total_stock_value, out_of_stock_count = await asyncio.gather(
        _scalar_int(db, total_products_sql, params),
        _scalar(db, stock_value_sql, params),
        _scalar_int(db, out_of_stock_sql, params),
    )

    low_result = await db.execute(text(low_stock_sql), params)
    low_items = [
        LowStockAlertItem(
            product_id=row.product_id,
            product_name=row.product_name,
            variation_id=row.variation_id,
            branch_id=row.branch_id,
            branch_name=row.branch_name,
            current_qty=_dec(row.current_qty),
            min_stock_level=_dec(row.min_stock_level),
            shortage=_dec(row.shortage),
        )
        for row in low_result
    ]

    return InventoryInsightsResponse(
        total_products=total_products,
        total_stock_value=total_stock_value,
        low_stock_count=len(low_items),
        out_of_stock_count=out_of_stock_count,
        low_stock_items=low_items,
    )


async def get_customer_insights(
    db: AsyncSession,
    business_id: UUID,
    limit: int = 50,
    sort_by: str = "total_spent",
) -> list[CustomerInsightItem]:
    order_map = {
        "total_spent": "total_spent DESC",
        "days_inactive": "days_since_last_purchase DESC NULLS LAST",
        "balance": "outstanding_balance ASC",
    }
    order_clause = order_map.get(sort_by, "total_spent DESC")
    params = {"business_id": business_id, "limit": limit}

    sql = f"""
        SELECT
            c.id AS customer_id,
            c.name AS customer_name,
            c.phone,
            COUNT(DISTINCT s.id) FILTER (
                WHERE s.id IS NOT NULL AND {_SALE_ACTIVE}
            ) AS total_purchases,
            COALESCE(SUM({_line_revenue_expr()}) FILTER (
                WHERE s.id IS NOT NULL AND {_SALE_ACTIVE}
            ), 0) AS total_spent,
            COALESCE(ledger.balance, 0) AS outstanding_balance,
            MAX(s.sold_at) FILTER (
                WHERE s.id IS NOT NULL AND {_SALE_ACTIVE}
            )::date AS last_purchase_date,
            CASE
                WHEN MAX(s.sold_at) FILTER (
                    WHERE s.id IS NOT NULL AND {_SALE_ACTIVE}
                ) IS NOT NULL
                THEN CURRENT_DATE - MAX(s.sold_at) FILTER (
                    WHERE s.id IS NOT NULL AND {_SALE_ACTIVE}
                )::date
                ELSE NULL
            END AS days_since_last_purchase
        FROM customers c
        LEFT JOIN sales s ON s.customer_id = c.id
            AND s.business_id = c.business_id
            AND s.deleted_at IS NULL
        LEFT JOIN sale_lines sl ON sl.sale_id = s.id
            AND sl.business_id = c.business_id
            AND sl.deleted_at IS NULL
        LEFT JOIN (
            SELECT customer_id, SUM(amount) AS balance
            FROM customer_ledger
            WHERE business_id = :business_id
                AND deleted_at IS NULL
            GROUP BY customer_id
        ) ledger ON ledger.customer_id = c.id
        WHERE c.business_id = :business_id
            AND c.deleted_at IS NULL
        GROUP BY c.id, c.name, c.phone, ledger.balance
        ORDER BY {order_clause}
        LIMIT :limit
    """
    result = await db.execute(text(sql), params)
    items: list[CustomerInsightItem] = []
    for row in result:
        total_spent = _dec(row.total_spent)
        purchases = int(row.total_purchases or 0)
        avg = total_spent / purchases if purchases > 0 else _ZERO
        items.append(
            CustomerInsightItem(
                customer_id=row.customer_id,
                customer_name=row.customer_name,
                phone=row.phone,
                total_purchases=purchases,
                total_spent=total_spent,
                outstanding_balance=_dec(row.outstanding_balance),
                last_purchase_date=row.last_purchase_date,
                avg_order_value=avg.quantize(Decimal("0.01")),
                days_since_last_purchase=(
                    int(row.days_since_last_purchase)
                    if row.days_since_last_purchase is not None
                    else None
                ),
            )
        )
    return items


async def get_expense_summary(
    db: AsyncSession,
    business_id: UUID,
    date_from: date,
    date_to: date,
    branch_id: UUID | None = None,
) -> list[ExpenseSummaryItem]:
    branch_sql = ""
    params: dict = {
        "business_id": business_id,
        "date_from": date_from,
        "date_to": date_to,
    }
    if branch_id is not None:
        params["branch_id"] = branch_id
        branch_sql = " AND e.branch_id = :branch_id"

    sql = f"""
        WITH category_stats AS (
            SELECT
                ec.id AS category_id,
                ec.name AS category_name,
                COALESCE(SUM(e.amount + e.tax_amount), 0) AS total_amount,
                COUNT(*) AS transaction_count
            FROM expenses e
            INNER JOIN expense_categories ec ON ec.id = e.expense_category_id
            WHERE e.business_id = :business_id
                AND e.deleted_at IS NULL
                AND ec.deleted_at IS NULL
                AND e.expense_date BETWEEN :date_from AND :date_to
                {branch_sql}
            GROUP BY ec.id, ec.name
        ),
        grand AS (
            SELECT COALESCE(SUM(total_amount), 0) AS total FROM category_stats
        )
        SELECT
            cs.category_id,
            cs.category_name,
            cs.total_amount,
            cs.transaction_count,
            CASE
                WHEN g.total > 0
                THEN ROUND(cs.total_amount / g.total * 100, 2)
                ELSE 0
            END AS percentage_of_total
        FROM category_stats cs
        CROSS JOIN grand g
        ORDER BY cs.total_amount DESC
    """
    result = await db.execute(text(sql), params)
    return [
        ExpenseSummaryItem(
            category_id=row.category_id,
            category_name=row.category_name,
            total_amount=_dec(row.total_amount),
            transaction_count=int(row.transaction_count or 0),
            percentage_of_total=_dec(row.percentage_of_total),
        )
        for row in result
    ]


async def get_profit_loss(
    db: AsyncSession,
    business_id: UUID,
    date_from: date,
    date_to: date,
    branch_id: UUID | None = None,
) -> ProfitLossResponse:
    branch_sql = _branch_clause(branch_id, "s")
    params: dict = {
        "business_id": business_id,
        "date_from": date_from,
        "date_to": date_to,
    }
    if branch_id is not None:
        params["branch_id"] = branch_id

    revenue_sql = f"""
        SELECT
            COALESCE(SUM({_line_revenue_expr()}), 0) AS revenue,
            COALESCE(SUM({_line_cost_expr()}), 0) AS cogs
        {_base_sale_joins()}
            {branch_sql}
            AND s.sold_at::date BETWEEN :date_from AND :date_to
    """
    rev_result = await db.execute(text(revenue_sql), params)
    rev_row = rev_result.one()
    total_revenue = _dec(rev_row.revenue)
    total_cogs = _dec(rev_row.cogs)
    gross_profit = total_revenue - total_cogs

    expense_breakdown = await get_expense_summary(
        db, business_id, date_from, date_to, branch_id
    )
    total_expenses = sum((item.total_amount for item in expense_breakdown), _ZERO)
    net_profit = gross_profit - total_expenses

    return ProfitLossResponse(
        period_start=date_from,
        period_end=date_to,
        total_revenue=total_revenue,
        total_cogs=total_cogs,
        gross_profit=gross_profit,
        gross_margin_pct=_margin_pct(gross_profit, total_revenue),
        total_expenses=total_expenses,
        net_profit=net_profit,
        net_margin_pct=_margin_pct(net_profit, total_revenue),
        expense_breakdown=expense_breakdown,
    )


def _change_pct(current: Decimal, previous: Decimal) -> Decimal:
    if previous <= _ZERO:
        return _ZERO
    return ((current - previous) / previous * _HUNDRED).quantize(Decimal("0.01"))


def _hour_label(hour: int) -> str:
    return f"{hour:02d}:00"


async def _daily_sale_metrics(
    db: AsyncSession,
    business_id: UUID,
    target_date: date,
    branch_id: UUID | None = None,
) -> tuple[Decimal, int, Decimal, Decimal]:
    branch_sql = _branch_clause(branch_id, "s")
    params: dict = {"business_id": business_id, "target_date": target_date}
    if branch_id is not None:
        params["branch_id"] = branch_id

    base = f"""
        {_base_sale_joins()}
            {branch_sql}
            AND s.sold_at::date = :target_date
    """
    revenue_sql = f"SELECT COALESCE(SUM({_line_revenue_expr()}), 0) {base}"
    cost_sql = f"SELECT COALESCE(SUM({_line_cost_expr()}), 0) {base}"
    tx_sql = f"SELECT COUNT(DISTINCT s.id) {base}"

    revenue, cost, transactions = await asyncio.gather(
        _scalar(db, revenue_sql, params),
        _scalar(db, cost_sql, params),
        _scalar_int(db, tx_sql, params),
    )
    profit = revenue - cost
    avg_order = revenue / transactions if transactions > 0 else _ZERO
    return revenue, transactions, profit, avg_order.quantize(Decimal("0.01"))


async def get_today_vs_yesterday(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
) -> TodayVsYesterdayResponse:
    today = _today()
    yesterday = today - timedelta(days=1)

    (
        (today_revenue, today_transactions, today_profit, today_avg),
        (yesterday_revenue, yesterday_transactions, yesterday_profit, yesterday_avg),
    ) = await asyncio.gather(
        _daily_sale_metrics(db, business_id, today, branch_id),
        _daily_sale_metrics(db, business_id, yesterday, branch_id),
    )

    return TodayVsYesterdayResponse(
        today_revenue=today_revenue,
        yesterday_revenue=yesterday_revenue,
        revenue_change_pct=_change_pct(today_revenue, yesterday_revenue),
        today_transactions=today_transactions,
        yesterday_transactions=yesterday_transactions,
        transaction_change_pct=_change_pct(
            Decimal(today_transactions), Decimal(yesterday_transactions)
        ),
        today_profit=today_profit,
        yesterday_profit=yesterday_profit,
        profit_change_pct=_change_pct(today_profit, yesterday_profit),
        today_avg_order=today_avg,
        yesterday_avg_order=yesterday_avg,
    )


async def get_peak_hours(
    db: AsyncSession,
    business_id: UUID,
    date_from: date,
    date_to: date,
    branch_id: UUID | None = None,
) -> list[PeakHourItem]:
    branch_sql = _branch_clause(branch_id, "s")
    params: dict = {
        "business_id": business_id,
        "date_from": date_from,
        "date_to": date_to,
    }
    if branch_id is not None:
        params["branch_id"] = branch_id

    sql = f"""
        SELECT
            EXTRACT(HOUR FROM s.sold_at AT TIME ZONE 'Asia/Karachi')::int AS hour,
            COALESCE(SUM({_line_revenue_expr()}), 0) AS total_revenue,
            COUNT(DISTINCT s.id) AS transaction_count
        {_base_sale_joins()}
            {branch_sql}
            AND s.sold_at::date BETWEEN :date_from AND :date_to
        GROUP BY hour
    """
    result = await db.execute(text(sql), params)
    by_hour: dict[int, tuple[Decimal, int]] = {}
    for row in result:
        by_hour[int(row.hour)] = (_dec(row.total_revenue), int(row.transaction_count or 0))

    items: list[PeakHourItem] = []
    for hour in range(24):
        revenue, tx_count = by_hour.get(hour, (_ZERO, 0))
        avg = revenue / tx_count if tx_count > 0 else _ZERO
        items.append(
            PeakHourItem(
                hour=hour,
                hour_label=_hour_label(hour),
                total_revenue=revenue,
                transaction_count=tx_count,
                avg_order_value=avg.quantize(Decimal("0.01")),
            )
        )
    items.sort(key=lambda x: x.transaction_count, reverse=True)
    return items


async def get_peak_days(
    db: AsyncSession,
    business_id: UUID,
    date_from: date,
    date_to: date,
    branch_id: UUID | None = None,
) -> list[PeakDayItem]:
    branch_sql = _branch_clause(branch_id, "s")
    params: dict = {
        "business_id": business_id,
        "date_from": date_from,
        "date_to": date_to,
    }
    if branch_id is not None:
        params["branch_id"] = branch_id

    sql = f"""
        SELECT
            EXTRACT(DOW FROM s.sold_at AT TIME ZONE 'Asia/Karachi')::int AS day_of_week,
            COALESCE(SUM({_line_revenue_expr()}), 0) AS total_revenue,
            COUNT(DISTINCT s.id) AS transaction_count
        {_base_sale_joins()}
            {branch_sql}
            AND s.sold_at::date BETWEEN :date_from AND :date_to
        GROUP BY day_of_week
    """
    result = await db.execute(text(sql), params)
    by_day: dict[int, tuple[Decimal, int]] = {}
    for row in result:
        by_day[int(row.day_of_week)] = (
            _dec(row.total_revenue),
            int(row.transaction_count or 0),
        )

    items: list[PeakDayItem] = []
    for dow in range(7):
        revenue, tx_count = by_day.get(dow, (_ZERO, 0))
        avg = revenue / tx_count if tx_count > 0 else _ZERO
        items.append(
            PeakDayItem(
                day_of_week=dow,
                day_name=_DAY_NAMES[dow],
                total_revenue=revenue,
                transaction_count=tx_count,
                avg_order_value=avg.quantize(Decimal("0.01")),
            )
        )
    items.sort(key=lambda x: x.transaction_count, reverse=True)
    return items


async def get_product_movement(
    db: AsyncSession,
    business_id: UUID,
    date_from: date,
    date_to: date,
    branch_id: UUID | None = None,
    dead_stock_days: int = 30,
) -> list[ProductMovementItem]:
    today = _today()
    params: dict = {
        "business_id": business_id,
        "date_from": date_from,
        "date_to": date_to,
    }
    branch_filter = ""
    sale_branch = ""
    if branch_id is not None:
        params["branch_id"] = branch_id
        branch_filter = " AND sm.branch_id = :branch_id"
        sale_branch = " AND s.branch_id = :branch_id"

    sql = f"""
        WITH stock_products AS (
            SELECT sm.product_id, sm.variation_id
            FROM stock_movements sm
            WHERE sm.business_id = :business_id
                AND sm.deleted_at IS NULL
                {branch_filter}
            GROUP BY sm.product_id, sm.variation_id
            HAVING COALESCE(SUM(sm.qty), 0) > 0
        ),
        sales_stats AS (
            SELECT
                sl.product_id,
                sl.variation_id,
                COALESCE(SUM(sl.qty), 0) AS total_qty_sold,
                COALESCE(SUM({_line_revenue_expr()}), 0) AS total_revenue,
                MAX(s.sold_at::date) AS last_sale_date
            FROM sale_lines sl
            INNER JOIN sales s ON s.id = sl.sale_id
                AND s.business_id = sl.business_id
            WHERE sl.business_id = :business_id
                AND sl.deleted_at IS NULL
                AND s.deleted_at IS NULL
                AND {_SALE_ACTIVE}
                AND s.sold_at::date BETWEEN :date_from AND :date_to
                {sale_branch}
            GROUP BY sl.product_id, sl.variation_id
        )
        SELECT
            sp.product_id,
            p.name AS product_name,
            pv.name AS variation_name,
            COALESCE(ss.total_qty_sold, 0) AS total_qty_sold,
            COALESCE(ss.total_revenue, 0) AS total_revenue,
            ss.last_sale_date
        FROM stock_products sp
        INNER JOIN products p ON p.id = sp.product_id
            AND p.business_id = :business_id
        LEFT JOIN product_variations pv ON pv.id = sp.variation_id
        LEFT JOIN sales_stats ss ON ss.product_id = sp.product_id
            AND ss.variation_id IS NOT DISTINCT FROM sp.variation_id
        ORDER BY COALESCE(ss.total_qty_sold, 0) DESC
    """
    result = await db.execute(text(sql), params)
    items: list[ProductMovementItem] = []
    for row in result:
        last_sale = row.last_sale_date
        if last_sale is None:
            days_since = 999
            category = "dead_stock"
        else:
            days_since = (today - last_sale).days
            if days_since <= 7:
                category = "fast_moving"
            elif days_since <= dead_stock_days:
                category = "slow_moving"
            else:
                category = "dead_stock"

        items.append(
            ProductMovementItem(
                product_id=row.product_id,
                product_name=row.product_name,
                variation_name=row.variation_name,
                total_qty_sold=_dec(row.total_qty_sold),
                total_revenue=_dec(row.total_revenue),
                last_sale_date=last_sale,
                days_since_last_sale=days_since,
                movement_category=category,
            )
        )
    return items


async def get_dead_stock(
    db: AsyncSession,
    business_id: UUID,
    dead_stock_days: int = 30,
    branch_id: UUID | None = None,
) -> list[DeadStockItem]:
    today = _today()
    cutoff = today - timedelta(days=dead_stock_days)
    params: dict = {
        "business_id": business_id,
        "cutoff": cutoff,
    }
    branch_filter = ""
    sale_branch = ""
    if branch_id is not None:
        params["branch_id"] = branch_id
        branch_filter = " AND sq.branch_id = :branch_id"
        sale_branch = " AND s.branch_id = :branch_id"

    sql = f"""
        WITH stock_qty AS (
            SELECT
                sm.branch_id,
                sm.product_id,
                sm.variation_id,
                COALESCE(SUM(sm.qty), 0) AS current_qty
            FROM stock_movements sm
            WHERE sm.business_id = :business_id
                AND sm.deleted_at IS NULL
            GROUP BY sm.branch_id, sm.product_id, sm.variation_id
            HAVING COALESCE(SUM(sm.qty), 0) > 0
        ),
        avg_costs AS (
            SELECT
                pl.product_id,
                pl.variation_id,
                AVG(pl.cost_per_unit) AS avg_cost
            FROM purchase_lines pl
            WHERE pl.business_id = :business_id
                AND pl.deleted_at IS NULL
            GROUP BY pl.product_id, pl.variation_id
        ),
        last_sales AS (
            SELECT
                sl.product_id,
                sl.variation_id,
                s.branch_id,
                MAX(s.sold_at::date) AS last_sale_date
            FROM sale_lines sl
            INNER JOIN sales s ON s.id = sl.sale_id
                AND s.business_id = sl.business_id
            WHERE sl.business_id = :business_id
                AND sl.deleted_at IS NULL
                AND s.deleted_at IS NULL
                AND {_SALE_ACTIVE}
                {sale_branch}
            GROUP BY sl.product_id, sl.variation_id, s.branch_id
        )
        SELECT
            sq.product_id,
            p.name AS product_name,
            pv.name AS variation_name,
            sq.branch_id,
            b.name AS branch_name,
            sq.current_qty,
            ls.last_sale_date,
            COALESCE(ac.avg_cost, 0) AS avg_cost
        FROM stock_qty sq
        INNER JOIN products p ON p.id = sq.product_id
            AND p.business_id = :business_id
        INNER JOIN branches b ON b.id = sq.branch_id
            AND b.business_id = :business_id
        LEFT JOIN product_variations pv ON pv.id = sq.variation_id
        LEFT JOIN avg_costs ac ON ac.product_id = sq.product_id
            AND ac.variation_id IS NOT DISTINCT FROM sq.variation_id
        LEFT JOIN last_sales ls ON ls.product_id = sq.product_id
            AND ls.variation_id IS NOT DISTINCT FROM sq.variation_id
            AND ls.branch_id = sq.branch_id
        WHERE (ls.last_sale_date IS NULL OR ls.last_sale_date < :cutoff)
            {branch_filter}
        ORDER BY sq.current_qty * COALESCE(ac.avg_cost, 0) DESC
    """
    result = await db.execute(text(sql), params)
    items: list[DeadStockItem] = []
    for row in result:
        last_sale = row.last_sale_date
        days_since = (today - last_sale).days if last_sale else 999
        qty = _dec(row.current_qty)
        avg_cost = _dec(row.avg_cost)
        items.append(
            DeadStockItem(
                product_id=row.product_id,
                product_name=row.product_name,
                variation_name=row.variation_name,
                branch_id=row.branch_id,
                branch_name=row.branch_name,
                current_qty=qty,
                last_sale_date=last_sale,
                days_since_last_sale=days_since,
                stock_value=(qty * avg_cost).quantize(Decimal("0.01")),
            )
        )
    return items


async def get_tax_summary(
    db: AsyncSession,
    business_id: UUID,
    date_from: date,
    date_to: date,
    branch_id: UUID | None = None,
) -> TaxSummaryResponse:
    branch_sql = _branch_clause(branch_id, "s")
    params: dict = {
        "business_id": business_id,
        "date_from": date_from,
        "date_to": date_to,
    }
    if branch_id is not None:
        params["branch_id"] = branch_id

    sales_totals_sql = f"""
        SELECT
            COALESCE(SUM({_line_revenue_expr()}), 0) AS taxable_sales,
            COALESCE(SUM(sl.tax_amount), 0) AS tax_collected
        {_base_sale_joins()}
            {branch_sql}
            AND s.sold_at::date BETWEEN :date_from AND :date_to
    """
    rate_sql = f"""
        SELECT
            COALESCE(sl.tax_rate, 0) AS tax_rate,
            COALESCE(SUM({_line_revenue_expr()}), 0) AS total_sales_amount,
            COALESCE(SUM(sl.tax_amount), 0) AS total_tax_amount,
            COUNT(DISTINCT s.id) AS transaction_count
        {_base_sale_joins()}
            {branch_sql}
            AND s.sold_at::date BETWEEN :date_from AND :date_to
        GROUP BY sl.tax_rate
        ORDER BY sl.tax_rate
    """
    purchase_branch = ""
    if branch_id is not None:
        purchase_branch = " AND po.branch_id = :branch_id"
    purchase_sql = f"""
        SELECT
            COALESCE(SUM(pl.ordered_qty * pl.cost_per_unit), 0) AS taxable_purchases,
            COALESCE(
                SUM(pl.ordered_qty * pl.cost_per_unit * pl.tax_rate / 100),
                0
            ) AS tax_paid
        FROM purchase_lines pl
        INNER JOIN purchase_orders po ON po.id = pl.purchase_order_id
            AND po.business_id = pl.business_id
        WHERE pl.business_id = :business_id
            AND pl.deleted_at IS NULL
            AND po.deleted_at IS NULL
            AND po.status != 'cancelled'
            AND COALESCE(po.ordered_at, po.created_at)::date
                BETWEEN :date_from AND :date_to
            {purchase_branch}
    """

    sales_result, rate_result, purchase_result = await asyncio.gather(
        db.execute(text(sales_totals_sql), params),
        db.execute(text(rate_sql), params),
        db.execute(text(purchase_sql), params),
    )
    sales_row = sales_result.one()
    purchase_row = purchase_result.one()

    tax_by_rate = [
        TaxByRateItem(
            tax_rate=_dec(row.tax_rate),
            total_sales_amount=_dec(row.total_sales_amount),
            total_tax_amount=_dec(row.total_tax_amount),
            transaction_count=int(row.transaction_count or 0),
        )
        for row in rate_result
    ]

    return TaxSummaryResponse(
        period_start=date_from,
        period_end=date_to,
        total_taxable_sales=_dec(sales_row.taxable_sales),
        total_tax_collected=_dec(sales_row.tax_collected),
        tax_by_rate=tax_by_rate,
        total_taxable_purchases=_dec(purchase_row.taxable_purchases),
        total_tax_paid=_dec(purchase_row.tax_paid),
    )


async def get_recent_transactions(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
    limit: int = 10,
) -> list[RecentTransactionItem]:
    branch_sql = _branch_clause(branch_id, "s")
    params: dict = {"business_id": business_id, "limit": limit}
    if branch_id is not None:
        params["branch_id"] = branch_id

    sql = f"""
        SELECT
            s.id AS sale_id,
            s.sale_number,
            s.sale_type,
            s.status,
            s.sold_at,
            b.name AS branch_name,
            c.name AS customer_name,
            u.full_name AS cashier_name,
            COALESCE(SUM({_line_revenue_expr()}), 0) AS total_amount,
            COALESCE(
                ARRAY_AGG(DISTINCT sp.payment_method)
                    FILTER (WHERE sp.payment_method IS NOT NULL),
                ARRAY[]::varchar[]
            ) AS payment_methods
        FROM sales s
        INNER JOIN branches b ON b.id = s.branch_id
            AND b.business_id = s.business_id
        INNER JOIN users u ON u.id = s.created_by
        LEFT JOIN customers c ON c.id = s.customer_id
            AND c.business_id = s.business_id
        LEFT JOIN sale_lines sl ON sl.sale_id = s.id
            AND sl.business_id = s.business_id
            AND sl.deleted_at IS NULL
        LEFT JOIN sale_payments sp ON sp.sale_id = s.id
            AND sp.business_id = s.business_id
            AND sp.deleted_at IS NULL
        WHERE s.business_id = :business_id
            AND s.deleted_at IS NULL
            AND {_SALE_ACTIVE}
            {branch_sql}
        GROUP BY
            s.id, s.sale_number, s.sale_type, s.status, s.sold_at,
            b.name, c.name, u.full_name
        ORDER BY s.sold_at DESC
        LIMIT :limit
    """
    result = await db.execute(text(sql), params)
    return [
        RecentTransactionItem(
            sale_id=row.sale_id,
            sale_number=row.sale_number,
            sale_type=row.sale_type,
            status=row.status,
            sold_at=row.sold_at,
            branch_name=row.branch_name,
            customer_name=row.customer_name,
            cashier_name=row.cashier_name,
            total_amount=_dec(row.total_amount),
            payment_methods=list(row.payment_methods or []),
        )
        for row in result
    ]


async def get_cash_in_drawer(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
) -> CashInDrawerResponse:
    params: dict = {"business_id": business_id}
    shift_sql = """
        SELECT
            rs.id AS shift_id,
            rs.branch_id,
            rs.opening_float,
            cr.name AS cash_register_name
        FROM register_shifts rs
        INNER JOIN cash_registers cr ON cr.id = rs.cash_register_id
            AND cr.business_id = rs.business_id
        WHERE rs.business_id = :business_id
            AND rs.status = 'open'
            AND rs.deleted_at IS NULL
    """
    if branch_id is not None:
        params["branch_id"] = branch_id
        shift_sql += " AND rs.branch_id = :branch_id"
    shift_sql += " ORDER BY rs.opened_at DESC LIMIT 1"

    shift_result = await db.execute(text(shift_sql), params)
    shift_row = shift_result.first()
    if shift_row is None:
        return CashInDrawerResponse(
            has_active_shift=False,
            shift_id=None,
            cash_register_name=None,
            opening_float=_ZERO,
            cash_sales_total=_ZERO,
            cash_returns_total=_ZERO,
            cash_in_total=_ZERO,
            cash_out_total=_ZERO,
            cash_expenses_total=_ZERO,
            expected_cash=_ZERO,
            branch_id=branch_id,
        )

    tx_params = {
        "business_id": business_id,
        "shift_id": shift_row.shift_id,
    }
    tx_sql = """
        SELECT
            COALESCE(SUM(CASE
                WHEN tx_type = 'sale' AND payment_method = 'cash'
                THEN amount ELSE 0 END), 0) AS cash_sales,
            COALESCE(SUM(CASE
                WHEN tx_type = 'sale_return' AND payment_method = 'cash'
                THEN amount ELSE 0 END), 0) AS cash_returns,
            COALESCE(SUM(CASE
                WHEN tx_type = 'cash_in' THEN amount ELSE 0 END), 0) AS cash_in,
            COALESCE(SUM(CASE
                WHEN tx_type = 'cash_out' THEN amount ELSE 0 END), 0) AS cash_out,
            COALESCE(SUM(CASE
                WHEN tx_type = 'expense' AND payment_method = 'cash'
                THEN amount ELSE 0 END), 0) AS cash_expenses
        FROM register_transactions
        WHERE business_id = :business_id
            AND register_shift_id = :shift_id
            AND deleted_at IS NULL
    """
    tx_result = await db.execute(text(tx_sql), tx_params)
    tx_row = tx_result.one()

    opening_float = _dec(shift_row.opening_float)
    cash_sales = _dec(tx_row.cash_sales)
    cash_returns = _dec(tx_row.cash_returns)
    cash_in = _dec(tx_row.cash_in)
    cash_out = _dec(tx_row.cash_out)
    cash_expenses = _dec(tx_row.cash_expenses)
    expected = (
        opening_float + cash_sales - cash_returns + cash_in - cash_out - cash_expenses
    )

    return CashInDrawerResponse(
        has_active_shift=True,
        shift_id=shift_row.shift_id,
        cash_register_name=shift_row.cash_register_name,
        opening_float=opening_float,
        cash_sales_total=cash_sales,
        cash_returns_total=cash_returns,
        cash_in_total=cash_in,
        cash_out_total=cash_out,
        cash_expenses_total=cash_expenses,
        expected_cash=expected.quantize(Decimal("0.01")),
        branch_id=shift_row.branch_id,
    )


async def get_enhanced_dashboard(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID | None = None,
) -> EnhancedDashboardResponse:
    today = _today()
    yesterday = today - timedelta(days=1)
    month_start = today.replace(day=1)
    branch_sql = _branch_clause(branch_id, "s")
    params: dict = {"business_id": business_id, "today": today, "yesterday": yesterday}
    if branch_id is not None:
        params["branch_id"] = branch_id

    month_params = dict(params)
    month_params["month_start"] = month_start

    today_base = f"""
        {_base_sale_joins()}
            {branch_sql}
            AND s.sold_at::date = :today
    """
    yesterday_revenue_sql = f"""
        SELECT COALESCE(SUM({_line_revenue_expr()}), 0)
        {_base_sale_joins()}
            {branch_sql}
            AND s.sold_at::date = :yesterday
    """
    month_from = f"""
        {_base_sale_joins()}
            {branch_sql}
            AND s.sold_at::date >= :month_start
    """
    today_revenue_sql = f"SELECT COALESCE(SUM({_line_revenue_expr()}), 0) {today_base}"
    today_cost_sql = f"SELECT COALESCE(SUM({_line_cost_expr()}), 0) {today_base}"
    today_tx_sql = f"SELECT COUNT(DISTINCT s.id) {today_base}"
    month_revenue_sql = f"SELECT COALESCE(SUM({_line_revenue_expr()}), 0) {month_from}"
    month_cost_sql = f"SELECT COALESCE(SUM({_line_cost_expr()}), 0) {month_from}"

    expense_branch = ""
    if branch_id is not None:
        expense_branch = " AND e.branch_id = :branch_id"
    today_expenses_sql = f"""
        SELECT COALESCE(SUM(e.amount + e.tax_amount), 0)
        FROM expenses e
        WHERE e.business_id = :business_id
            AND e.deleted_at IS NULL
            AND e.expense_date = :today
            {expense_branch}
    """

    low_stock_sql = """
        SELECT COUNT(*)
        FROM product_locations pl
        INNER JOIN branches b ON b.id = pl.branch_id
        LEFT JOIN LATERAL (
            SELECT COALESCE(SUM(sm.qty), 0) AS current_qty
            FROM stock_movements sm
            WHERE sm.business_id = pl.business_id
                AND sm.branch_id = pl.branch_id
                AND sm.product_id = pl.product_id
                AND sm.variation_id IS NOT DISTINCT FROM pl.variation_id
                AND sm.deleted_at IS NULL
        ) stock ON TRUE
        WHERE pl.business_id = :business_id
            AND pl.deleted_at IS NULL
            AND pl.min_stock_level IS NOT NULL
            AND stock.current_qty < pl.min_stock_level
    """
    out_of_stock_sql = """
        SELECT COUNT(*)
        FROM product_locations pl
        INNER JOIN branches b ON b.id = pl.branch_id
        LEFT JOIN LATERAL (
            SELECT COALESCE(SUM(sm.qty), 0) AS current_qty
            FROM stock_movements sm
            WHERE sm.business_id = pl.business_id
                AND sm.branch_id = pl.branch_id
                AND sm.product_id = pl.product_id
                AND sm.variation_id IS NOT DISTINCT FROM pl.variation_id
                AND sm.deleted_at IS NULL
        ) stock ON TRUE
        WHERE pl.business_id = :business_id
            AND pl.deleted_at IS NULL
            AND stock.current_qty <= 0
    """
    if branch_id is not None:
        low_stock_sql += " AND pl.branch_id = :branch_id"
        out_of_stock_sql += " AND pl.branch_id = :branch_id"

    open_shifts_sql = """
        SELECT COUNT(*)
        FROM register_shifts rs
        WHERE rs.business_id = :business_id
            AND rs.status = 'open'
            AND rs.deleted_at IS NULL
    """
    if branch_id is not None:
        open_shifts_sql += " AND rs.branch_id = :branch_id"

    customer_bal_sql = """
        SELECT COALESCE(SUM(ABS(sub.balance)), 0)
        FROM (
            SELECT SUM(cl.amount) AS balance
            FROM customer_ledger cl
            WHERE cl.business_id = :business_id
                AND cl.deleted_at IS NULL
            GROUP BY cl.customer_id
            HAVING SUM(cl.amount) < 0
        ) sub
    """
    supplier_bal_sql = """
        SELECT COALESCE(SUM(ABS(sub.balance)), 0)
        FROM (
            SELECT SUM(sl.amount) AS balance
            FROM supplier_ledger sl
            WHERE sl.business_id = :business_id
                AND sl.deleted_at IS NULL
            GROUP BY sl.supplier_id
            HAVING SUM(sl.amount) < 0
        ) sub
    """

    (
        today_revenue,
        today_cost,
        today_transactions,
        yesterday_revenue,
        month_revenue,
        month_cost,
        today_expenses,
        low_stock_alerts,
        out_of_stock_count,
        open_shifts,
        pending_customer,
        pending_supplier,
        cash_drawer,
        recent,
    ) = await asyncio.gather(
        _scalar(db, today_revenue_sql, params),
        _scalar(db, today_cost_sql, params),
        _scalar_int(db, today_tx_sql, params),
        _scalar(db, yesterday_revenue_sql, params),
        _scalar(db, month_revenue_sql, month_params),
        _scalar(db, month_cost_sql, month_params),
        _scalar(db, today_expenses_sql, params),
        _scalar_int(db, low_stock_sql, params),
        _scalar_int(db, out_of_stock_sql, params),
        _scalar_int(db, open_shifts_sql, params),
        _scalar(db, customer_bal_sql, params),
        _scalar(db, supplier_bal_sql, params),
        get_cash_in_drawer(db, business_id, branch_id),
        get_recent_transactions(db, business_id, branch_id, limit=5),
    )

    today_profit = today_revenue - today_cost
    month_profit = month_revenue - month_cost
    today_avg = (
        today_revenue / today_transactions if today_transactions > 0 else _ZERO
    ).quantize(Decimal("0.01"))

    return EnhancedDashboardResponse(
        today_revenue=today_revenue,
        today_transactions=today_transactions,
        today_profit=today_profit,
        today_avg_order=today_avg,
        today_expenses=today_expenses,
        this_month_revenue=month_revenue,
        this_month_profit=month_profit,
        revenue_vs_yesterday_pct=_change_pct(today_revenue, yesterday_revenue),
        low_stock_alerts=low_stock_alerts,
        out_of_stock_count=out_of_stock_count,
        open_shifts=open_shifts,
        pending_customer_receivables=pending_customer,
        pending_supplier_payables=pending_supplier,
        cash_in_drawer=cash_drawer.expected_cash,
        recent_transactions=recent,
    )
