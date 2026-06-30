"""Suppliers, supplier ledger, and expenses permissions (migrated from test_batch5)."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.tenants import build_rbac_tenant

pytestmark = pytest.mark.integration


def test_ap_module_permissions(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Batch5 {unique_suffix}",
        owner_name="Batch5 Owner",
        phone_prefix="350",
    )
    suffix = tenant.suffix
    owner_headers = tenant.owner_headers
    manager_headers = tenant.manager_headers
    cashier_headers = tenant.cashier_headers
    branch_id = tenant.branch_id
    today = date.today().isoformat()
    assert manager_headers is not None and cashier_headers is not None

    read_cases = [
        (
            "GET /suppliers (manager)",
            manager_headers,
            f"{api_base}/suppliers",
            200,
        ),
        (
            "GET /suppliers (cashier, denied)",
            cashier_headers,
            f"{api_base}/suppliers",
            403,
        ),
        (
            "GET /expenses (manager)",
            manager_headers,
            f"{api_base}/expenses",
            200,
        ),
        (
            "GET /expenses (cashier, denied)",
            cashier_headers,
            f"{api_base}/expenses",
            403,
        ),
    ]
    for label, headers, url, expected in read_cases:
        assert_status(http_client.get(url, headers=headers), expected, label=label)

    supplier = http_client.post(
        f"{api_base}/suppliers",
        headers=manager_headers,
        json={"name": f"Supplier {suffix}"},
    )
    assert_status(supplier, 201, label="POST /suppliers (manager)")
    assert_status(
        http_client.post(
            f"{api_base}/suppliers",
            headers=cashier_headers,
            json={"name": f"Blocked {suffix}"},
        ),
        403,
        label="POST /suppliers (cashier, denied)",
    )
    supplier_id = supplier.json()["id"] if supplier.status_code == 201 else None

    if supplier_id:
        assert_status(
            http_client.get(
                f"{api_base}/supplier-ledger/{supplier_id}/balance",
                headers=manager_headers,
            ),
            200,
            label="GET /supplier-ledger/{id}/balance (manager)",
        )
        assert_status(
            http_client.get(
                f"{api_base}/supplier-ledger/{supplier_id}/balance",
                headers=cashier_headers,
            ),
            403,
            label="GET /supplier-ledger/{id}/balance (cashier, denied)",
        )
        assert_status(
            http_client.post(
                f"{api_base}/supplier-ledger/{supplier_id}/payment",
                headers=manager_headers,
                json={
                    "amount": "25.00",
                    "payment_method": "cash",
                },
            ),
            201,
            label="POST /supplier-ledger/{id}/payment (manager)",
        )
        assert_status(
            http_client.post(
                f"{api_base}/supplier-ledger/{supplier_id}/payment",
                headers=cashier_headers,
                json={
                    "amount": "25.00",
                    "payment_method": "cash",
                },
            ),
            403,
            label="POST /supplier-ledger/{id}/payment (cashier, denied)",
        )

    category = http_client.post(
        f"{api_base}/expenses/categories",
        headers=manager_headers,
        json={"name": f"Utilities {suffix}"},
    )
    assert_status(category, 201, label="POST /expenses/categories (manager)")
    assert_status(
        http_client.post(
            f"{api_base}/expenses/categories",
            headers=cashier_headers,
            json={"name": f"Blocked Cat {suffix}"},
        ),
        403,
        label="POST /expenses/categories (cashier, denied)",
    )
    category_id = category.json()["id"] if category.status_code == 201 else None

    expense_id = None
    if category_id:
        expense = http_client.post(
            f"{api_base}/expenses",
            headers=manager_headers,
            json={
                "branch_id": branch_id,
                "expense_category_id": category_id,
                "expense_date": today,
                "amount": "100.00",
                "payments": [
                    {
                        "payment_method": "cash",
                        "amount": "100.00",
                    }
                ],
            },
        )
        assert_status(expense, 201, label="POST /expenses (manager)")
        assert_status(
            http_client.post(
                f"{api_base}/expenses",
                headers=cashier_headers,
                json={
                    "branch_id": branch_id,
                    "expense_category_id": category_id,
                    "expense_date": today,
                    "amount": "50.00",
                },
            ),
            403,
            label="POST /expenses (cashier, denied)",
        )
        if expense.status_code == 201:
            expense_id = expense.json()["id"]

    if expense_id:
        assert_status(
            http_client.delete(
                f"{api_base}/expenses/{expense_id}",
                headers=manager_headers,
            ),
            403,
            label="DELETE /expenses/{id} (manager, denied)",
        )
        assert_status(
            http_client.delete(
                f"{api_base}/expenses/{expense_id}",
                headers=owner_headers,
            ),
            200,
            label="DELETE /expenses/{id} (owner)",
        )

    if supplier_id:
        supplier_del = http_client.post(
            f"{api_base}/suppliers",
            headers=manager_headers,
            json={"name": f"To Delete {suffix}"},
        )
        if supplier_del.status_code == 201:
            del_id = supplier_del.json()["id"]
            assert_status(
                http_client.delete(
                    f"{api_base}/suppliers/{del_id}",
                    headers=manager_headers,
                ),
                200,
                label="DELETE /suppliers/{id} (manager)",
            )
            assert_status(
                http_client.delete(
                    f"{api_base}/suppliers/{supplier_id}",
                    headers=cashier_headers,
                ),
                403,
                label="DELETE /suppliers/{id} (cashier, denied)",
            )
