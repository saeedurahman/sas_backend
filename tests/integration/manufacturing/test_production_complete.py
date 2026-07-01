"""Phase 4 — production complete with FIFO IN/OUT."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.db import db_scalar
from tests.helpers.tenants import (
    add_stock_adjustment,
    build_rbac_tenant,
    create_product,
    create_supplier,
    create_unit,
)

pytestmark = pytest.mark.integration


def _enable_manufacturing(
    client: httpx.Client,
    api_base: str,
    headers: dict[str, str],
) -> None:
    response = client.put(
        f"{api_base}/business/config",
        headers=headers,
        json={"config_json": {"enable_manufacturing": True}},
    )
    assert_status(response, 200, label="enable enable_manufacturing")


def _create_manufactured_product(
    client: httpx.Client,
    tenant: object,
    *,
    name: str,
) -> str:
    response = client.post(
        tenant.url("/products"),  # type: ignore[attr-defined]
        headers=tenant.owner_headers,  # type: ignore[attr-defined]
        json={
            "name": name,
            "base_unit_id": tenant.unit_id,  # type: ignore[attr-defined]
            "product_type": "manufactured",
            "tracking_type": "none",
        },
    )
    assert_status(response, 201, label=f"create manufactured product {name}")
    return response.json()["id"]


def _create_bom(
    client: httpx.Client,
    api_base: str,
    headers: dict[str, str],
    *,
    finished_id: str,
    ingredient_id: str,
    name: str,
    qty_required: str = "2",
    output_qty: str = "1",
    wastage_pct: str = "0",
) -> str:
    response = client.post(
        f"{api_base}/manufacturing/boms",
        headers=headers,
        json={
            "product_id": finished_id,
            "name": name,
            "output_qty": output_qty,
            "is_active": True,
            "lines": [
                {
                    "ingredient_product_id": ingredient_id,
                    "qty_required": qty_required,
                    "wastage_pct": wastage_pct,
                },
            ],
        },
    )
    assert_status(response, 201, label="create BOM")
    return response.json()["id"]


def _seed_fifo_stock(
    client: httpx.Client,
    api_base: str,
    tenant: object,
    *,
    product_id: str,
    qty: str,
    cost_per_unit: str,
) -> None:
    supplier_id = create_supplier(client, tenant)  # type: ignore[arg-type]
    po_resp = client.post(
        f"{api_base}/purchases/orders",
        headers=tenant.owner_headers,  # type: ignore[attr-defined]
        json={
            "supplier_id": supplier_id,
            "branch_id": tenant.branch_id,  # type: ignore[attr-defined]
            "lines": [
                {
                    "product_id": product_id,
                    "ordered_qty": qty,
                    "cost_per_unit": cost_per_unit,
                }
            ],
        },
    )
    assert_status(po_resp, 201, label="create purchase order for FIFO seed")
    po_body = po_resp.json()
    purchase_line_id = po_body["lines"][0]["id"]

    receipt_resp = client.post(
        f"{api_base}/purchases/receipts",
        headers=tenant.owner_headers,  # type: ignore[attr-defined]
        json={
            "branch_id": tenant.branch_id,  # type: ignore[attr-defined]
            "supplier_id": supplier_id,
            "purchase_order_id": po_body["id"],
            "lines": [
                {
                    "product_id": product_id,
                    "purchase_line_id": purchase_line_id,
                    "qty_received": qty,
                    "cost_per_unit": cost_per_unit,
                }
            ],
        },
    )
    assert_status(receipt_resp, 201, label="receive GRN for FIFO seed")


def _stock_qty(
    business_id: str,
    branch_id: str,
    product_id: str,
) -> Decimal:
    value = db_scalar(
        """
        SELECT COALESCE(SUM(qty), 0)
        FROM stock_movements
        WHERE business_id = :business_id
          AND branch_id = :branch_id
          AND product_id = :product_id
          AND deleted_at IS NULL
        """,
        {
            "business_id": business_id,
            "branch_id": branch_id,
            "product_id": product_id,
        },
    )
    return Decimal(str(value))


def _setup_tenant(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
    *,
    phone_prefix: str,
    business_name: str,
) -> object:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=business_name,
        owner_name="Manufacturing Owner",
        phone_prefix=phone_prefix,
    )
    create_unit(http_client, tenant)
    _enable_manufacturing(http_client, api_base, tenant.owner_headers)
    return tenant


def _create_started_order(
    http_client: httpx.Client,
    api_base: str,
    tenant: object,
    *,
    bom_id: str,
    qty_to_produce: str,
) -> str:
    create_resp = http_client.post(
        f"{api_base}/manufacturing/production-orders",
        headers=tenant.owner_headers,  # type: ignore[attr-defined]
        json={
            "branch_id": tenant.branch_id,  # type: ignore[attr-defined]
            "bom_header_id": bom_id,
            "qty_to_produce": qty_to_produce,
        },
    )
    assert_status(create_resp, 201, label="create production order")
    order_id = create_resp.json()["id"]

    start_resp = http_client.post(
        f"{api_base}/manufacturing/production-orders/{order_id}/start",
        headers=tenant.owner_headers,  # type: ignore[attr-defined]
    )
    assert_status(start_resp, 200, label="start production order")
    return order_id


def test_production_complete_fifo_in_out_full_batch(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="640",
        business_name=f"Production Complete {unique_suffix}",
    )
    suffix = tenant.suffix

    flour_id = create_product(http_client, tenant, name=f"Flour {suffix}")
    bread_id = _create_manufactured_product(
        http_client, tenant, name=f"Bread {suffix}"
    )
    _seed_fifo_stock(
        http_client,
        api_base,
        tenant,
        product_id=flour_id,
        qty="100",
        cost_per_unit="5.00",
    )
    bom_id = _create_bom(
        http_client,
        api_base,
        tenant.owner_headers,
        finished_id=bread_id,
        ingredient_id=flour_id,
        name=f"Bread BOM {suffix}",
    )
    order_id = _create_started_order(
        http_client,
        api_base,
        tenant,
        bom_id=bom_id,
        qty_to_produce="10",
    )

    complete_resp = http_client.post(
        f"{api_base}/manufacturing/production-orders/{order_id}/complete",
        headers=tenant.owner_headers,
        json={"qty_produced": "10"},
    )
    assert_status(complete_resp, 200, label="complete production order")
    completed = complete_resp.json()
    assert completed["status"] == "completed"
    assert completed["qty_produced"] == "10.0000"
    assert completed["completed_at"] is not None
    assert len(completed["lines"]) == 1
    assert completed["lines"][0]["qty_consumed"] == "20.0000"
    assert completed["lines"][0]["cost_per_unit"] == "5.00"

    assert _stock_qty(tenant.business_id, tenant.branch_id, flour_id) == Decimal("80")
    assert _stock_qty(tenant.business_id, tenant.branch_id, bread_id) == Decimal("10")

    finished_layer = db_scalar(
        """
        SELECT qty_remaining
        FROM purchase_lines
        WHERE business_id = :business_id
          AND production_order_id = :order_id
          AND product_id = :product_id
          AND deleted_at IS NULL
        """,
        {
            "business_id": tenant.business_id,
            "order_id": order_id,
            "product_id": bread_id,
        },
    )
    assert Decimal(str(finished_layer)) == Decimal("10")

    flour_remaining = db_scalar(
        """
        SELECT COALESCE(SUM(qty_remaining), 0)
        FROM purchase_lines
        WHERE business_id = :business_id
          AND product_id = :product_id
          AND deleted_at IS NULL
        """,
        {
            "business_id": tenant.business_id,
            "product_id": flour_id,
        },
    )
    assert Decimal(str(flour_remaining)) == Decimal("80")

    finished_cost = db_scalar(
        """
        SELECT cost_per_unit
        FROM purchase_lines
        WHERE business_id = :business_id
          AND production_order_id = :order_id
          AND product_id = :product_id
          AND deleted_at IS NULL
        """,
        {
            "business_id": tenant.business_id,
            "order_id": order_id,
            "product_id": bread_id,
        },
    )
    assert Decimal(str(finished_cost)) == Decimal("10.00")

    out_count = db_scalar(
        """
        SELECT COUNT(*)
        FROM stock_movements sm
        JOIN production_lines pl ON pl.id = sm.reference_id
        WHERE sm.business_id = :business_id
          AND sm.movement_type = 'production_out'
          AND sm.reference_type = 'production_line'
          AND pl.production_order_id = :order_id
          AND sm.deleted_at IS NULL
        """,
        {
            "business_id": tenant.business_id,
            "order_id": order_id,
        },
    )
    in_count = db_scalar(
        """
        SELECT COUNT(*)
        FROM stock_movements
        WHERE business_id = :business_id
          AND movement_type = 'production_in'
          AND reference_type = 'production_order'
          AND reference_id = :order_id
          AND deleted_at IS NULL
        """,
        {
            "business_id": tenant.business_id,
            "order_id": order_id,
        },
    )
    assert int(out_count) >= 1
    assert int(in_count) == 1


def test_production_complete_partial_qty(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="641",
        business_name=f"Production Partial {unique_suffix}",
    )
    suffix = tenant.suffix

    flour_id = create_product(http_client, tenant, name=f"Sugar {suffix}")
    candy_id = _create_manufactured_product(
        http_client, tenant, name=f"Candy {suffix}"
    )
    add_stock_adjustment(http_client, tenant, product_id=flour_id, qty_delta="50")
    bom_id = _create_bom(
        http_client,
        api_base,
        tenant.owner_headers,
        finished_id=candy_id,
        ingredient_id=flour_id,
        name=f"Candy BOM {suffix}",
    )
    order_id = _create_started_order(
        http_client,
        api_base,
        tenant,
        bom_id=bom_id,
        qty_to_produce="10",
    )

    complete_resp = http_client.post(
        f"{api_base}/manufacturing/production-orders/{order_id}/complete",
        headers=tenant.owner_headers,
        json={"qty_produced": "4"},
    )
    assert_status(complete_resp, 200, label="partial complete")
    completed = complete_resp.json()
    assert completed["status"] == "completed"
    assert completed["qty_produced"] == "4.0000"
    assert completed["lines"][0]["qty_consumed"] == "8.0000"
    assert _stock_qty(tenant.business_id, tenant.branch_id, flour_id) == Decimal("42")
    assert _stock_qty(tenant.business_id, tenant.branch_id, candy_id) == Decimal("4")


def test_production_complete_rejects_insufficient_stock_and_draft(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="642",
        business_name=f"Production Stock Guard {unique_suffix}",
    )
    suffix = tenant.suffix

    flour_id = create_product(http_client, tenant, name=f"Cocoa {suffix}")
    bar_id = _create_manufactured_product(
        http_client, tenant, name=f"Chocolate Bar {suffix}"
    )
    bom_id = _create_bom(
        http_client,
        api_base,
        tenant.owner_headers,
        finished_id=bar_id,
        ingredient_id=flour_id,
        name=f"Bar BOM {suffix}",
    )

    create_resp = http_client.post(
        f"{api_base}/manufacturing/production-orders",
        headers=tenant.owner_headers,
        json={
            "branch_id": tenant.branch_id,
            "bom_header_id": bom_id,
            "qty_to_produce": "5",
        },
    )
    assert_status(create_resp, 201, label="create order")
    order_id = create_resp.json()["id"]

    draft_complete = http_client.post(
        f"{api_base}/manufacturing/production-orders/{order_id}/complete",
        headers=tenant.owner_headers,
        json={"qty_produced": "5"},
    )
    assert_status(draft_complete, 400, label="complete draft without start")

    http_client.post(
        f"{api_base}/manufacturing/production-orders/{order_id}/start",
        headers=tenant.owner_headers,
    )
    no_stock = http_client.post(
        f"{api_base}/manufacturing/production-orders/{order_id}/complete",
        headers=tenant.owner_headers,
        json={"qty_produced": "5"},
    )
    assert_status(no_stock, 400, label="complete without ingredient stock")
    assert "stock" in no_stock.json()["detail"].lower()


def test_production_complete_permissions(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="643",
        business_name=f"Production Complete Perms {unique_suffix}",
    )
    suffix = tenant.suffix

    flour_id = create_product(http_client, tenant, name=f"Milk {suffix}")
    yogurt_id = _create_manufactured_product(
        http_client, tenant, name=f"Yogurt {suffix}"
    )
    add_stock_adjustment(http_client, tenant, product_id=flour_id, qty_delta="20")
    bom_id = _create_bom(
        http_client,
        api_base,
        tenant.owner_headers,
        finished_id=yogurt_id,
        ingredient_id=flour_id,
        name=f"Yogurt BOM {suffix}",
    )
    order_id = _create_started_order(
        http_client,
        api_base,
        tenant,
        bom_id=bom_id,
        qty_to_produce="2",
    )

    assert tenant.cashier_headers is not None
    cashier_complete = http_client.post(
        f"{api_base}/manufacturing/production-orders/{order_id}/complete",
        headers=tenant.cashier_headers,
        json={"qty_produced": "2"},
    )
    assert_status(cashier_complete, 403, label="cashier complete")

    manager_complete = http_client.post(
        f"{api_base}/manufacturing/production-orders/{order_id}/complete",
        headers=tenant.manager_headers,
        json={"qty_produced": "2"},
    )
    assert_status(manager_complete, 200, label="manager complete")
    assert manager_complete.json()["status"] == "completed"
