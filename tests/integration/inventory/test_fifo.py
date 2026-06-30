"""FIFO layer consumption and restoration (migrated from test_fifo_consumption)."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

from tests.helpers.db import db_rows, db_scalar
from tests.helpers.records import assert_ok
from tests.helpers.tenants import TenantContext, add_stock_adjustment, build_fifo_tenant

pytestmark = pytest.mark.integration


def _fifo_ctx(tenant: TenantContext) -> dict:
    return {
        "headers": tenant.owner_headers,
        "branch_id": tenant.branch_id,
        "business_id": tenant.business_id,
        "product_id": tenant.product_id,
        "supplier_id": tenant.supplier_id,
    }


def _create_po_with_lines(
    client: httpx.Client,
    api_base: str,
    ctx: dict,
    layers: list[tuple[str, str]],
) -> list[dict]:
    lines = [
        {
            "product_id": ctx["product_id"],
            "ordered_qty": qty,
            "cost_per_unit": cost,
        }
        for qty, cost in layers
    ]
    po = client.post(
        f"{api_base}/purchases/orders",
        headers=ctx["headers"],
        json={
            "supplier_id": ctx["supplier_id"],
            "branch_id": ctx["branch_id"],
            "lines": lines,
        },
    )
    if po.status_code != 201:
        raise RuntimeError(f"PO create failed: {po.status_code} {po.text}")
    po_body = po.json()
    po_id = po_body["id"]
    po_lines = po_body.get("lines") or client.get(
        f"{api_base}/purchases/orders/{po_id}",
        headers=ctx["headers"],
    ).json()["lines"]

    for pl, (qty, cost) in zip(po_lines, layers, strict=True):
        receipt = client.post(
            f"{api_base}/purchases/receipts",
            headers=ctx["headers"],
            json={
                "branch_id": ctx["branch_id"],
                "supplier_id": ctx["supplier_id"],
                "purchase_order_id": po_id,
                "lines": [
                    {
                        "product_id": ctx["product_id"],
                        "purchase_line_id": pl["id"],
                        "qty_received": qty,
                        "cost_per_unit": cost,
                    }
                ],
            },
        )
        if receipt.status_code != 201:
            raise RuntimeError(f"receipt failed: {receipt.status_code}")

    return db_rows(
        """
        SELECT id, qty_remaining, cost_per_unit, received_qty
        FROM purchase_lines
        WHERE business_id = :business_id
          AND product_id = :product_id
          AND deleted_at IS NULL
        ORDER BY created_at ASC, id ASC
        """,
        {
            "business_id": ctx["business_id"],
            "product_id": ctx["product_id"],
        },
    )


def _create_sale(
    client: httpx.Client,
    api_base: str,
    ctx: dict,
    qty: str,
    payments: list[dict] | None = None,
) -> httpx.Response:
    if payments is None:
        payments = [{"payment_method": "cash", "amount": str(Decimal(qty) * 100)}]
    return client.post(
        f"{api_base}/sales",
        headers=ctx["headers"],
        json={
            "branch_id": ctx["branch_id"],
            "lines": [
                {
                    "product_id": ctx["product_id"],
                    "qty": qty,
                    "unit_price": "100.00",
                }
            ],
            "payments": payments,
        },
    )


def _get_sale_line_id(sale_body: dict) -> str:
    return sale_body["lines"][0]["id"]


def _qty_remaining_for_cost(ctx: dict, cost: str) -> Decimal:
    val = db_scalar(
        """
        SELECT qty_remaining
        FROM purchase_lines
        WHERE business_id = :business_id
          AND product_id = :product_id
          AND cost_per_unit = :cost
          AND deleted_at IS NULL
        LIMIT 1
        """,
        {
            "business_id": ctx["business_id"],
            "product_id": ctx["product_id"],
            "cost": cost,
        },
    )
    return Decimal(str(val))


def _fifo_cost_sql(ctx: dict, qty: str) -> Decimal | None:
    val = db_scalar(
        "SELECT get_fifo_cost(:bid, :pid, NULL::uuid, :qty)",
        {
            "bid": ctx["business_id"],
            "pid": ctx["product_id"],
            "qty": qty,
        },
    )
    return Decimal(str(val)) if val is not None else None


def test_single_layer_sale(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_fifo_tenant(http_client, api_base, unique_suffix)
    ctx = _fifo_ctx(tenant)
    add_stock_adjustment(http_client, tenant, qty_delta="20")
    plines = _create_po_with_lines(http_client, api_base, ctx, [("10", "100.00")])
    pl_id = plines[0]["id"]

    sale = _create_sale(http_client, api_base, ctx, "3")
    assert_ok("single-layer sale HTTP 201", sale.status_code == 201, str(sale.status_code))
    remaining = db_scalar(
        "SELECT qty_remaining FROM purchase_lines WHERE id = :id",
        {"id": pl_id},
    )
    assert_ok(
        "single-layer qty_remaining after sale",
        Decimal(str(remaining)) == Decimal("7"),
        f"remaining={remaining}",
    )
    moves = db_rows(
        """
        SELECT purchase_line_id, qty
        FROM stock_movements
        WHERE business_id = :business_id
          AND movement_type = 'sale'
          AND deleted_at IS NULL
        ORDER BY created_at DESC
        LIMIT 5
        """,
        {"business_id": ctx["business_id"]},
    )
    traced = [m for m in moves if str(m["purchase_line_id"]) == str(pl_id)]
    assert_ok(
        "single-layer traced movement",
        len(traced) == 1 and Decimal(str(traced[0]["qty"])) == Decimal("-3"),
        str(traced),
    )


def test_multi_layer_sale(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_fifo_tenant(http_client, api_base, unique_suffix + 1)
    ctx = _fifo_ctx(tenant)
    add_stock_adjustment(http_client, tenant, qty_delta="20")
    _create_po_with_lines(http_client, api_base, ctx, [("5", "100.00"), ("3", "120.00")])
    expected_cost = _fifo_cost_sql(ctx, "8")
    sale = _create_sale(http_client, api_base, ctx, "8")
    assert_ok("multi-layer sale HTTP 201", sale.status_code == 201, str(sale.status_code))
    if sale.status_code == 201:
        actual_cost = Decimal(str(sale.json()["lines"][0]["cost_per_unit"]))
        assert_ok(
            "multi-layer weighted cost matches get_fifo_cost",
            expected_cost is not None and actual_cost == expected_cost,
            f"expected={expected_cost} actual={actual_cost}",
        )
    rem_100 = _qty_remaining_for_cost(ctx, "100.00")
    rem_120 = _qty_remaining_for_cost(ctx, "120.00")
    assert_ok(
        "multi-layer both layers depleted",
        rem_100 == Decimal("0") and rem_120 == Decimal("0"),
        f"layer_100={rem_100} layer_120={rem_120}",
    )


def test_insufficient_layers(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_fifo_tenant(http_client, api_base, unique_suffix + 2)
    ctx = _fifo_ctx(tenant)
    add_stock_adjustment(http_client, tenant, qty_delta="20")
    _create_po_with_lines(http_client, api_base, ctx, [("2", "100.00")])

    sale = _create_sale(http_client, api_base, ctx, "5")
    assert_ok(
        "insufficient layers sale succeeds",
        sale.status_code == 201,
        str(sale.status_code),
    )
    notif_count = db_scalar(
        """
        SELECT COUNT(*)
        FROM notification_log
        WHERE business_id = :business_id
          AND deleted_at IS NULL
          AND notification_type = 'system'
          AND payload_json->>'alert_kind' = 'fifo_insufficient_layers'
        """,
        {"business_id": ctx["business_id"]},
    )
    assert_ok(
        "insufficient layers notification created",
        int(notif_count or 0) >= 1,
        f"count={notif_count}",
    )


def test_full_cancel_multi_layer(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_fifo_tenant(http_client, api_base, unique_suffix + 3)
    ctx = _fifo_ctx(tenant)
    add_stock_adjustment(http_client, tenant, qty_delta="20")
    _create_po_with_lines(http_client, api_base, ctx, [("5", "100.00"), ("3", "120.00")])
    draft = http_client.post(
        f"{api_base}/sales",
        headers=ctx["headers"],
        json={
            "branch_id": ctx["branch_id"],
            "lines": [
                {
                    "product_id": ctx["product_id"],
                    "qty": "8",
                    "unit_price": "100.00",
                }
            ],
            "payments": [],
        },
    )
    assert_ok("multi-layer draft sale", draft.status_code == 201, str(draft.status_code))
    if draft.status_code != 201:
        return
    sale_id = draft.json()["id"]
    cancel = http_client.put(
        f"{api_base}/sales/{sale_id}/cancel",
        headers=ctx["headers"],
    )
    assert_ok("full cancel HTTP 200", cancel.status_code == 200, str(cancel.status_code))
    rem_100 = _qty_remaining_for_cost(ctx, "100.00")
    rem_120 = _qty_remaining_for_cost(ctx, "120.00")
    assert_ok(
        "full cancel restores both layers",
        rem_100 == Decimal("5") and rem_120 == Decimal("3"),
        f"layer_100={rem_100} layer_120={rem_120}",
    )


def test_partial_return_multi_layer(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_fifo_tenant(http_client, api_base, unique_suffix + 4)
    ctx = _fifo_ctx(tenant)
    add_stock_adjustment(http_client, tenant, qty_delta="20")
    _create_po_with_lines(http_client, api_base, ctx, [("5", "100.00"), ("3", "120.00")])
    sale = _create_sale(http_client, api_base, ctx, "8")
    if sale.status_code != 201:
        assert_ok("partial return setup sale", False, str(sale.status_code))
        return
    sale_body = sale.json()
    sale_id = sale_body["id"]
    sale_line_id = _get_sale_line_id(sale_body)

    ret = http_client.post(
        f"{api_base}/returns",
        headers=ctx["headers"],
        json={
            "branch_id": ctx["branch_id"],
            "sale_id": sale_id,
            "lines": [
                {
                    "product_id": ctx["product_id"],
                    "sale_line_id": sale_line_id,
                    "qty": "4",
                    "unit_price": "100.00",
                }
            ],
            "refund_payments": [{"payment_method": "cash", "amount": "400.00"}],
        },
    )
    assert_ok("partial return HTTP 201", ret.status_code == 201, str(ret.status_code))
    if ret.status_code == 201:
        sale_moves = db_rows(
            """
            SELECT qty, cost_per_unit, movement_sequence
            FROM stock_movements
            WHERE reference_id = :sale_line_id
              AND movement_type = 'sale'
              AND deleted_at IS NULL
            ORDER BY movement_sequence ASC NULLS LAST, movement_at ASC, id ASC
            """,
            {"sale_line_id": sale_line_id},
        )
        traced_order = [str(m["cost_per_unit"]) for m in sale_moves]
        sequences = [m["movement_sequence"] for m in sale_moves]
        assert_ok(
            "partial return sale movement FIFO order",
            traced_order == ["100.00", "120.00"] and sequences == [0, 1],
            f"order={traced_order} sequences={sequences}",
        )
    rem_100 = _qty_remaining_for_cost(ctx, "100.00")
    rem_120 = _qty_remaining_for_cost(ctx, "120.00")
    assert_ok(
        "partial return LIFO restore (100=1, 120=3)",
        rem_100 == Decimal("1") and rem_120 == Decimal("3"),
        f"layer_100={rem_100} layer_120={rem_120}",
    )


def test_legacy_return_no_layer_restore(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_fifo_tenant(http_client, api_base, unique_suffix + 5)
    ctx = _fifo_ctx(tenant)
    add_stock_adjustment(http_client, tenant, qty_delta="10")
    sale = _create_sale(http_client, api_base, ctx, "2")
    assert_ok("legacy setup sale", sale.status_code == 201, str(sale.status_code))
    if sale.status_code != 201:
        return
    sale_body = sale.json()
    sale_line_id = _get_sale_line_id(sale_body)
    moves = db_rows(
        """
        SELECT purchase_line_id FROM stock_movements
        WHERE reference_id = :sale_line_id
          AND movement_type = 'sale'
          AND deleted_at IS NULL
        """,
        {"sale_line_id": sale_line_id},
    )
    assert_ok(
        "legacy sale has no traced layers",
        all(m["purchase_line_id"] is None for m in moves),
        str(moves),
    )
    ret = http_client.post(
        f"{api_base}/returns",
        headers=ctx["headers"],
        json={
            "branch_id": ctx["branch_id"],
            "sale_id": sale_body["id"],
            "lines": [
                {
                    "product_id": ctx["product_id"],
                    "sale_line_id": sale_line_id,
                    "qty": "1",
                    "unit_price": "100.00",
                }
            ],
            "refund_payments": [{"payment_method": "cash", "amount": "100.00"}],
        },
    )
    assert_ok(
        "legacy return succeeds",
        ret.status_code == 201,
        str(ret.status_code),
    )


def test_mixed_traced_untraced_partial_return(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_fifo_tenant(http_client, api_base, unique_suffix + 7)
    ctx = _fifo_ctx(tenant)
    add_stock_adjustment(http_client, tenant, qty_delta="20")
    _create_po_with_lines(http_client, api_base, ctx, [("2", "100.00")])

    sale = _create_sale(http_client, api_base, ctx, "5")
    assert_ok("mixed sale HTTP 201", sale.status_code == 201, str(sale.status_code))
    if sale.status_code != 201:
        return

    sale_body = sale.json()
    sale_id = sale_body["id"]
    sale_line_id = _get_sale_line_id(sale_body)

    sale_moves = db_rows(
        """
        SELECT qty, purchase_line_id
        FROM stock_movements
        WHERE reference_id = :sale_line_id
          AND movement_type = 'sale'
          AND deleted_at IS NULL
        """,
        {"sale_line_id": sale_line_id},
    )
    traced_sale_qty = sum(
        (
            abs(Decimal(str(m["qty"])))
            for m in sale_moves
            if m["purchase_line_id"] is not None
        ),
        Decimal("0"),
    )
    untraced_sale_qty = sum(
        (
            abs(Decimal(str(m["qty"])))
            for m in sale_moves
            if m["purchase_line_id"] is None
        ),
        Decimal("0"),
    )
    assert_ok(
        "mixed sale has traced + untraced consumption",
        traced_sale_qty == Decimal("2") and untraced_sale_qty == Decimal("3"),
        f"traced={traced_sale_qty} untraced={untraced_sale_qty}",
    )

    ret = http_client.post(
        f"{api_base}/returns",
        headers=ctx["headers"],
        json={
            "branch_id": ctx["branch_id"],
            "sale_id": sale_id,
            "lines": [
                {
                    "product_id": ctx["product_id"],
                    "sale_line_id": sale_line_id,
                    "qty": "4",
                    "unit_price": "100.00",
                }
            ],
            "refund_payments": [{"payment_method": "cash", "amount": "400.00"}],
        },
    )
    assert_ok("mixed partial return HTTP 201", ret.status_code == 201, str(ret.status_code))
    if ret.status_code != 201:
        return

    rem_100 = _qty_remaining_for_cost(ctx, "100.00")
    assert_ok(
        "mixed partial LIFO restores traced layers",
        rem_100 == Decimal("2"),
        f"layer_100={rem_100}",
    )

    return_line_id = ret.json()["lines"][0]["id"]
    return_moves = db_rows(
        """
        SELECT qty, purchase_line_id
        FROM stock_movements
        WHERE reference_type = 'sale_return_line'
          AND reference_id = :return_line_id
          AND deleted_at IS NULL
        """,
        {"return_line_id": return_line_id},
    )
    traced_return_qty = sum(
        (
            Decimal(str(m["qty"]))
            for m in return_moves
            if m["purchase_line_id"] is not None
        ),
        Decimal("0"),
    )
    legacy_return_qty = sum(
        (
            Decimal(str(m["qty"]))
            for m in return_moves
            if m["purchase_line_id"] is None
        ),
        Decimal("0"),
    )
    total_return_qty = traced_return_qty + legacy_return_qty
    assert_ok(
        "mixed partial traced return qty",
        traced_return_qty == Decimal("2"),
        f"traced_return={traced_return_qty}",
    )
    assert_ok(
        "mixed partial legacy return qty",
        legacy_return_qty == Decimal("2"),
        f"legacy_return={legacy_return_qty}",
    )
    assert_ok(
        "mixed partial total restored qty",
        total_return_qty == Decimal("4"),
        f"total={total_return_qty}",
    )


def test_concurrent_sales(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    from app.database import AsyncSessionLocal
    from app.services.fifo_service import consume_fifo_layers

    tenant = build_fifo_tenant(http_client, api_base, unique_suffix + 6)
    ctx = _fifo_ctx(tenant)
    add_stock_adjustment(http_client, tenant, qty_delta="20")
    _create_po_with_lines(http_client, api_base, ctx, [("5", "100.00")])
    business_id = UUID(str(ctx["business_id"]))
    product_id = UUID(str(ctx["product_id"]))
    outcomes: list[tuple[list, Decimal]] = []

    async def consume_three() -> None:
        async with AsyncSessionLocal() as db:
            async with db.begin():
                consumptions, shortfall = await consume_fifo_layers(
                    db,
                    business_id,
                    product_id,
                    None,
                    Decimal("3"),
                )
                outcomes.append((consumptions, shortfall))

    async def run_parallel() -> None:
        await asyncio.gather(consume_three(), consume_three())

    asyncio.run(run_parallel())

    total_consumed = sum(
        sum((c.qty for c in consumptions), Decimal("0"))
        for consumptions, _ in outcomes
    )
    remaining = _qty_remaining_for_cost(ctx, "100.00")
    assert_ok(
        "concurrent layer consumes both complete",
        len(outcomes) == 2,
        f"workers={len(outcomes)}",
    )
    assert_ok(
        "concurrent no double-consumption",
        total_consumed == Decimal("5") and remaining == Decimal("0"),
        f"consumed={total_consumed} remaining={remaining}",
    )
