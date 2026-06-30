"""Sale void and cancel integration tests (subset migrated from test_sale_void)."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.db import db_scalar
from tests.helpers.sales import create_sale_payload

pytestmark = pytest.mark.integration


def test_void_completed_cash_sale(
    http_client: httpx.Client,
    api_base: str,
    pos_tenant,
) -> None:
    tenant = pos_tenant
    assert tenant.manager_headers is not None
    assert tenant.product_id is not None
    assert tenant.shift_id is not None

    sale = http_client.post(
        f"{api_base}/sales",
        headers=tenant.manager_headers,
        json=create_sale_payload(
            branch_id=tenant.branch_id,
            product_id=tenant.product_id,
            register_shift_id=tenant.shift_id,
        ),
    )
    assert_status(sale, 201, label="cash void setup sale")
    sale_id = sale.json()["id"]

    summary_before = http_client.get(
        f"{api_base}/shifts/{tenant.shift_id}/summary",
        headers=tenant.owner_headers,
    )
    expected_before = Decimal(str(summary_before.json()["expected_cash"]))

    void = http_client.post(
        f"{api_base}/sales/{sale_id}/void",
        headers=tenant.manager_headers,
    )
    assert_status(void, 200, label="void cash sale HTTP 200")
    assert void.json()["status"] == "voided"

    summary_after = http_client.get(
        f"{api_base}/shifts/{tenant.shift_id}/summary",
        headers=tenant.owner_headers,
    )
    expected_after = Decimal(str(summary_after.json()["expected_cash"]))
    assert expected_after == expected_before - Decimal("100.00"), (
        f"before={expected_before} after={expected_after}"
    )

    rem = db_scalar(
        """
        SELECT COALESCE(SUM(qty), 0)
        FROM stock_movements
        WHERE business_id = :business_id
          AND product_id = :product_id
          AND deleted_at IS NULL
        """,
        {"business_id": tenant.business_id, "product_id": tenant.product_id},
    )
    assert Decimal(str(rem)) == Decimal("100"), f"net_qty={rem}"


def test_void_blocked_when_shift_closed(
    http_client: httpx.Client,
    api_base: str,
    pos_tenant,
) -> None:
    tenant = pos_tenant
    assert tenant.manager_headers is not None
    assert tenant.product_id is not None

    reg2 = http_client.post(
        f"{api_base}/registers",
        headers=tenant.owner_headers,
        json={"name": "Reg2", "branch_id": tenant.branch_id},
    ).json()["id"]
    shift2_resp = http_client.post(
        f"{api_base}/shifts/open",
        headers=tenant.owner_headers,
        json={"cash_register_id": reg2, "opening_float": "100.00"},
    )
    assert_status(shift2_resp, 201, label="closed shift setup open")
    shift2 = shift2_resp.json()["id"]

    sale = http_client.post(
        f"{api_base}/sales",
        headers=tenant.manager_headers,
        json=create_sale_payload(
            branch_id=tenant.branch_id,
            product_id=tenant.product_id,
            register_shift_id=shift2,
        ),
    )
    assert_status(sale, 201, label="closed shift setup sale")

    http_client.post(
        f"{api_base}/shifts/{shift2}/close",
        headers=tenant.owner_headers,
        json={"actual_cash": "200.00"},
    )
    void = http_client.post(
        f"{api_base}/sales/{sale.json()['id']}/void",
        headers=tenant.manager_headers,
    )
    assert_status(void, 400, label="void blocked when shift closed")


def test_partially_paid_cancel_reversal(
    http_client: httpx.Client,
    api_base: str,
    pos_tenant,
) -> None:
    tenant = pos_tenant
    assert tenant.manager_headers is not None
    assert tenant.product_id is not None
    assert tenant.shift_id is not None

    sale = http_client.post(
        f"{api_base}/sales",
        headers=tenant.manager_headers,
        json=create_sale_payload(
            branch_id=tenant.branch_id,
            product_id=tenant.product_id,
            register_shift_id=tenant.shift_id,
            unit_price="100.00",
            payments=[{"payment_method": "cash", "amount": "40.00"}],
        ),
    )
    assert_status(sale, 201, label="partially paid sale created")
    body = sale.json()
    assert body["status"] == "partially_paid"

    summary_before = http_client.get(
        f"{api_base}/shifts/{tenant.shift_id}/summary",
        headers=tenant.owner_headers,
    )
    expected_before = Decimal(str(summary_before.json()["expected_cash"]))

    cancel = http_client.put(
        f"{api_base}/sales/{body['id']}/cancel",
        headers=tenant.manager_headers,
    )
    assert_status(cancel, 200, label="partially paid cancel HTTP 200")

    summary_after = http_client.get(
        f"{api_base}/shifts/{tenant.shift_id}/summary",
        headers=tenant.owner_headers,
    )
    expected_after = Decimal(str(summary_after.json()["expected_cash"]))
    assert expected_after == expected_before - Decimal("40.00"), (
        f"before={expected_before} after={expected_after}"
    )

    pay_status = db_scalar(
        """
        SELECT status FROM sale_payments
        WHERE sale_id = :sale_id AND deleted_at IS NULL
        LIMIT 1
        """,
        {"sale_id": body["id"]},
    )
    assert pay_status == "refunded", str(pay_status)
