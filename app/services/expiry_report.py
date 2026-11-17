"""Expiry report: expired stock and stock expiring within N days, with value (qty x layer cost)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_CENT = Decimal("0.01")


def _money(v: Decimal) -> str:
    return str(v.quantize(_CENT, rounding=ROUND_HALF_UP))


async def expiry_rows(
    db: AsyncSession,
    business_id: UUID,
    *,
    within_days: int,
    branch_ids: list[UUID] | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    today = today or datetime.now(timezone.utc).date()
    where = [
        "pl.business_id = :b", "pl.deleted_at IS NULL", "pl.qty_remaining > 0",
        "pl.expiry_date IS NOT NULL", "pl.expiry_date <= :horizon",
    ]
    params: dict[str, Any] = {"b": business_id, "horizon": date.fromordinal(today.toordinal() + within_days)}
    if branch_ids is not None:
        where.append("pl.branch_id = ANY(CAST(:branches AS uuid[]))")
        params["branches"] = [str(x) for x in branch_ids]
    rows = (
        await db.execute(
            text(
                "SELECT pl.id AS layer_id, pl.product_id, p.name AS product, pv.name AS variation, "
                "p.sku, pl.branch_id, br.name AS branch, pl.batch_number AS batch_no, "
                "pl.expiry_date, pl.qty_remaining AS qty, pl.cost_per_unit "
                "FROM purchase_lines pl JOIN products p ON p.id = pl.product_id "
                "LEFT JOIN product_variations pv ON pv.id = pl.variation_id "
                "LEFT JOIN branches br ON br.id = pl.branch_id "
                f"WHERE {' AND '.join(where)} "  # noqa: S608
                "ORDER BY pl.expiry_date, p.name, pl.id"
            ),
            params,
        )
    ).mappings().all()
    out = []
    for r in rows:
        qty, cost = Decimal(str(r["qty"])), Decimal(str(r["cost_per_unit"]))
        variation = r["variation"]
        out.append(
            {
                "layer_id": r["layer_id"],
                "product_id": r["product_id"],
                "product": r["product"],
                "variation": None if variation in (None, "", "Default") else variation,
                "sku": r["sku"],
                "branch_id": r["branch_id"],
                "branch": r["branch"],
                "batch_no": r["batch_no"],
                "expiry_date": r["expiry_date"],
                "days_to_expiry": (r["expiry_date"] - today).days,
                "qty": qty,
                "cost_per_unit": cost,
                "value": qty * cost,
            }
        )
    return out


async def expiry_report(
    db: AsyncSession,
    business_id: UUID,
    *,
    within_days: int = 30,
    branch_ids: list[UUID] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    today = today or datetime.now(timezone.utc).date()
    rows = await expiry_rows(db, business_id, within_days=within_days, branch_ids=branch_ids, today=today)

    def fmt(r: dict[str, Any]) -> dict[str, Any]:
        return {
            **{k: (str(v) if k in ("layer_id", "product_id", "branch_id") and v else v)
               for k, v in r.items()},
            "expiry_date": r["expiry_date"].isoformat(),
            "qty": str(r["qty"]),
            "cost_per_unit": str(r["cost_per_unit"]),
            "value": _money(r["value"]),
        }

    expired = [r for r in rows if r["days_to_expiry"] < 0]
    near = [r for r in rows if r["days_to_expiry"] >= 0]
    zero = Decimal("0")
    return {
        "as_of": today.isoformat(),
        "within_days": within_days,
        "expired": [fmt(r) for r in expired],
        "near_expiry": [fmt(r) for r in near],
        "totals": {
            "expired_qty": str(sum((r["qty"] for r in expired), zero)),
            "expired_value": _money(sum((r["value"] for r in expired), zero)),
            "near_expiry_qty": str(sum((r["qty"] for r in near), zero)),
            "near_expiry_value": _money(sum((r["value"] for r in near), zero)),
        },
    }
