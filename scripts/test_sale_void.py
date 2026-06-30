#!/usr/bin/env python3
"""Sale void and enhanced cancel integration tests.

DEPRECATED: pytest coverage lives in tests/integration/sales/test_void_cancel.py
(void cash, void blocked on closed shift, partially_paid cancel). This script
retains the full 21-check suite for any assertions not yet migrated; run it
directly if you need that broader coverage. Prefer: pytest tests/integration/sales/
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.core.config import settings

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000/api/v1")
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f": {detail}"
    print(line)


def sync_db_url() -> str:
    url = settings.database_url
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    return url


def db_scalar(sql: str, params: dict) -> object | None:
    engine = create_engine(sync_db_url())
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def setup_tenant(client: httpx.Client, suffix: int) -> dict:
    password = "TestPass1"
    owner_phone = f"440{suffix:07d}"
    manager_phone = f"441{suffix:07d}"
    cashier_phone = f"442{suffix:07d}"

    reg = client.post(
        f"{BASE}/auth/register",
        json={
            "business_name": f"VoidTest {suffix}",
            "business_type_code": "retail",
            "owner_name": "Owner",
            "owner_phone": owner_phone,
            "owner_password": password,
            "branch_name": "Main",
        },
    )
    if reg.status_code != 201:
        raise RuntimeError(f"register failed: {reg.status_code}")
    body = reg.json()
    owner_headers = {"Authorization": f"Bearer {body['access_token']}"}
    branch_id = body["user"]["branch_id"]
    business_id = body["user"]["business_id"]

    roles = client.get(f"{BASE}/roles", headers=owner_headers).json()
    manager_role_id = next(r["id"] for r in roles if r["name"].lower() == "manager")
    cashier_role_id = next(r["id"] for r in roles if r["name"].lower() == "cashier")

    for phone, name, role_id in (
        (manager_phone, "Manager", manager_role_id),
        (cashier_phone, "Cashier", cashier_role_id),
    ):
        u = client.post(
            f"{BASE}/users",
            headers=owner_headers,
            json={
                "full_name": name,
                "phone": phone,
                "password": password,
                "role_ids": [role_id],
            },
        )
        if u.status_code != 201:
            raise RuntimeError(f"user create failed: {u.status_code}")

    manager_headers = {
        "Authorization": f"Bearer {client.post(f'{BASE}/auth/login', json={'phone': manager_phone, 'password': password}).json()['access_token']}"
    }
    cashier_headers = {
        "Authorization": f"Bearer {client.post(f'{BASE}/auth/login', json={'phone': cashier_phone, 'password': password}).json()['access_token']}"
    }

    unit_id = client.post(
        f"{BASE}/units",
        headers=owner_headers,
        json={"name": "Piece", "symbol": "pc", "is_base_unit": True},
    ).json()["id"]
    product_id = client.post(
        f"{BASE}/products",
        headers=owner_headers,
        json={
            "name": f"Product {suffix}",
            "base_unit_id": unit_id,
            "product_type": "standard",
            "tracking_type": "none",
        },
    ).json()["id"]

    register_id = client.post(
        f"{BASE}/registers",
        headers=owner_headers,
        json={"name": f"Reg {suffix}", "branch_id": branch_id},
    ).json()["id"]

    shift_id = client.post(
        f"{BASE}/shifts/open",
        headers=manager_headers,
        json={"cash_register_id": register_id, "opening_float": "500.00"},
    ).json()["id"]

    client.post(
        f"{BASE}/adjustments",
        headers=owner_headers,
        json={
            "branch_id": branch_id,
            "reason": "opening_balance",
            "lines": [
                {
                    "product_id": product_id,
                    "qty_delta": "100",
                    "cost_per_unit": "50.00",
                }
            ],
        },
    )

    return {
        "owner_headers": owner_headers,
        "manager_headers": manager_headers,
        "cashier_headers": cashier_headers,
        "branch_id": branch_id,
        "business_id": business_id,
        "product_id": product_id,
        "register_id": register_id,
        "shift_id": shift_id,
    }


def create_sale(
    client: httpx.Client,
    ctx: dict,
    *,
    headers: dict,
    qty: str = "1",
    unit_price: str = "100.00",
    payments: list[dict] | None = None,
    customer_id: str | None = None,
    shift_id: str | None = None,
) -> httpx.Response:
    if payments is None:
        payments = [{"payment_method": "cash", "amount": unit_price}]
    body = {
        "branch_id": ctx["branch_id"],
        "lines": [
            {
                "product_id": ctx["product_id"],
                "qty": qty,
                "unit_price": unit_price,
            }
        ],
        "payments": payments,
    }
    if customer_id:
        body["customer_id"] = customer_id
    if shift_id:
        body["register_shift_id"] = shift_id
    return client.post(f"{BASE}/sales", headers=headers, json=body)


def test_void_cash_sale(client: httpx.Client, ctx: dict) -> None:
    sale = create_sale(
        client,
        ctx,
        headers=ctx["manager_headers"],
        shift_id=ctx["shift_id"],
    )
    record("cash void setup sale", sale.status_code == 201, str(sale.status_code))
    if sale.status_code != 201:
        return
    sale_id = sale.json()["id"]

    summary_before = client.get(
        f"{BASE}/shifts/{ctx['shift_id']}/summary",
        headers=ctx["owner_headers"],
    )
    expected_before = Decimal(str(summary_before.json()["expected_cash"]))

    void = client.post(
        f"{BASE}/sales/{sale_id}/void",
        headers=ctx["manager_headers"],
    )
    record("void cash sale HTTP 200", void.status_code == 200, str(void.status_code))
    if void.status_code == 200:
        record(
            "void cash sale status=voided",
            void.json()["status"] == "voided",
            void.json()["status"],
        )

    summary_after = client.get(
        f"{BASE}/shifts/{ctx['shift_id']}/summary",
        headers=ctx["owner_headers"],
    )
    expected_after = Decimal(str(summary_after.json()["expected_cash"]))
    record(
        "void cash restores expected_cash",
        expected_after == expected_before - Decimal("100.00"),
        f"before={expected_before} after={expected_after}",
    )

    rem = db_scalar(
        """
        SELECT COALESCE(SUM(qty), 0)
        FROM stock_movements
        WHERE business_id = :business_id
          AND product_id = :product_id
          AND deleted_at IS NULL
        """,
        {"business_id": ctx["business_id"], "product_id": ctx["product_id"]},
    )
    record(
        "void cash restores stock",
        Decimal(str(rem)) == Decimal("100"),
        f"net_qty={rem}",
    )


def test_void_credit_sale(client: httpx.Client, ctx: dict) -> None:
    customer = client.post(
        f"{BASE}/customers",
        headers=ctx["manager_headers"],
        json={"name": "Credit Customer", "phone": f"99{uuid.uuid4().int % 10_000_000:07d}"},
    )
    customer_id = customer.json()["id"]

    balance_before = Decimal(
        str(
            client.get(
                f"{BASE}/customers/{customer_id}/balance",
                headers=ctx["manager_headers"],
            ).json()["balance"]
        )
    )

    sale = create_sale(
        client,
        ctx,
        headers=ctx["manager_headers"],
        shift_id=ctx["shift_id"],
        customer_id=customer_id,
        payments=[{"payment_method": "credit", "amount": "100.00"}],
    )
    record("credit void setup sale", sale.status_code == 201, str(sale.status_code))
    if sale.status_code != 201:
        return

    balance_after_sale = Decimal(
        str(
            client.get(
                f"{BASE}/customers/{customer_id}/balance",
                headers=ctx["manager_headers"],
            ).json()["balance"]
        )
    )

    void = client.post(
        f"{BASE}/sales/{sale.json()['id']}/void",
        headers=ctx["manager_headers"],
    )
    record("void credit sale HTTP 200", void.status_code == 200, str(void.status_code))

    balance_after_void = Decimal(
        str(
            client.get(
                f"{BASE}/customers/{customer_id}/balance",
                headers=ctx["manager_headers"],
            ).json()["balance"]
        )
    )
    record(
        "void credit restores customer balance",
        balance_before == balance_after_void
        and balance_after_sale == balance_before - Decimal("100.00"),
        f"before={balance_before} after_sale={balance_after_sale} after_void={balance_after_void}",
    )


def test_void_blocked_closed_shift(client: httpx.Client, ctx: dict) -> None:
    reg2 = client.post(
        f"{BASE}/registers",
        headers=ctx["owner_headers"],
        json={"name": "Reg2", "branch_id": ctx["branch_id"]},
    ).json()["id"]
    shift2_resp = client.post(
        f"{BASE}/shifts/open",
        headers=ctx["owner_headers"],
        json={"cash_register_id": reg2, "opening_float": "100.00"},
    )
    if shift2_resp.status_code != 201:
        record(
            "closed shift setup open",
            False,
            f"{shift2_resp.status_code} {shift2_resp.text[:200]}",
        )
        return
    shift2 = shift2_resp.json()["id"]
    sale = create_sale(
        client,
        ctx,
        headers=ctx["manager_headers"],
        shift_id=shift2,
    )
    if sale.status_code != 201:
        record("closed shift setup sale", False, str(sale.status_code))
        return
    client.post(
        f"{BASE}/shifts/{shift2}/close",
        headers=ctx["owner_headers"],
        json={"actual_cash": "200.00"},
    )
    void = client.post(
        f"{BASE}/sales/{sale.json()['id']}/void",
        headers=ctx["manager_headers"],
    )
    record(
        "void blocked when shift closed",
        void.status_code == 400,
        str(void.status_code),
    )


def test_void_blocked_with_returns(client: httpx.Client, ctx: dict) -> None:
    sale = create_sale(
        client,
        ctx,
        headers=ctx["manager_headers"],
        shift_id=ctx["shift_id"],
        qty="2",
        unit_price="50.00",
        payments=[{"payment_method": "cash", "amount": "100.00"}],
    )
    if sale.status_code != 201:
        record("return block setup sale", False, str(sale.status_code))
        return
    sale_body = sale.json()
    sale_line_id = sale_body["lines"][0]["id"]
    ret = client.post(
        f"{BASE}/returns",
        headers=ctx["manager_headers"],
        json={
            "branch_id": ctx["branch_id"],
            "sale_id": sale_body["id"],
            "lines": [
                {
                    "product_id": ctx["product_id"],
                    "sale_line_id": sale_line_id,
                    "qty": "1",
                    "unit_price": "50.00",
                }
            ],
            "refund_payments": [{"payment_method": "cash", "amount": "50.00"}],
        },
    )
    record("partial return setup", ret.status_code == 201, str(ret.status_code))
    void = client.post(
        f"{BASE}/sales/{sale_body['id']}/void",
        headers=ctx["manager_headers"],
    )
    record(
        "void blocked with existing returns",
        void.status_code == 400,
        str(void.status_code),
    )


def test_void_cashier_forbidden(client: httpx.Client, ctx: dict) -> None:
    sale = create_sale(
        client,
        ctx,
        headers=ctx["cashier_headers"],
        shift_id=ctx["shift_id"],
    )
    if sale.status_code != 201:
        record("cashier void setup sale", False, str(sale.status_code))
        return
    void = client.post(
        f"{BASE}/sales/{sale.json()['id']}/void",
        headers=ctx["cashier_headers"],
    )
    record("void as cashier denied", void.status_code == 403, str(void.status_code))


def test_partially_paid_cancel_reversal(client: httpx.Client, ctx: dict) -> None:
    sale = create_sale(
        client,
        ctx,
        headers=ctx["manager_headers"],
        shift_id=ctx["shift_id"],
        unit_price="100.00",
        payments=[{"payment_method": "cash", "amount": "40.00"}],
    )
    record("partially paid sale created", sale.status_code == 201, str(sale.status_code))
    if sale.status_code != 201:
        return
    body = sale.json()
    record(
        "partially paid status",
        body["status"] == "partially_paid",
        body["status"],
    )

    summary_before = client.get(
        f"{BASE}/shifts/{ctx['shift_id']}/summary",
        headers=ctx["owner_headers"],
    )
    expected_before = Decimal(str(summary_before.json()["expected_cash"]))

    cancel = client.put(
        f"{BASE}/sales/{body['id']}/cancel",
        headers=ctx["manager_headers"],
    )
    record("partially paid cancel HTTP 200", cancel.status_code == 200, str(cancel.status_code))

    summary_after = client.get(
        f"{BASE}/shifts/{ctx['shift_id']}/summary",
        headers=ctx["owner_headers"],
    )
    expected_after = Decimal(str(summary_after.json()["expected_cash"]))
    record(
        "partially paid cancel reverses register cash",
        expected_after == expected_before - Decimal("40.00"),
        f"before={expected_before} after={expected_after}",
    )

    pay_status = db_scalar(
        """
        SELECT status FROM sale_payments
        WHERE sale_id = :sale_id AND deleted_at IS NULL
        LIMIT 1
        """,
        {"sale_id": body["id"]},
    )
    record(
        "partially paid payment marked refunded",
        pay_status == "refunded",
        str(pay_status),
    )


def test_draft_cancel_unchanged(client: httpx.Client, ctx: dict) -> None:
    draft = client.post(
        f"{BASE}/sales",
        headers=ctx["manager_headers"],
        json={
            "branch_id": ctx["branch_id"],
            "lines": [
                {
                    "product_id": ctx["product_id"],
                    "qty": "1",
                    "unit_price": "100.00",
                }
            ],
            "payments": [],
        },
    )
    record("draft sale created", draft.status_code == 201, str(draft.status_code))
    if draft.status_code != 201:
        return
    draft_id = draft.json()["id"]
    cancel = client.put(
        f"{BASE}/sales/{draft_id}/cancel",
        headers=ctx["manager_headers"],
    )
    record("draft cancel HTTP 200", cancel.status_code == 200, str(cancel.status_code))
    pay_count = db_scalar(
        "SELECT COUNT(*) FROM sale_payments WHERE sale_id = :id",
        {"id": draft_id},
    )
    reg_count = db_scalar(
        """
        SELECT COUNT(*) FROM register_transactions
        WHERE reference_id = :id AND reference_type = 'sale'
        """,
        {"id": draft_id},
    )
    record(
        "draft cancel no payments/register txs",
        int(pay_count or 0) == 0 and int(reg_count or 0) == 0,
        f"payments={pay_count} register={reg_count}",
    )


def test_reporting_void_vs_return(client: httpx.Client, suffix: int) -> None:
    ctx = setup_tenant(client, suffix + 99)
    today = date.today().isoformat()

    sale_void = create_sale(
        client,
        ctx,
        headers=ctx["manager_headers"],
        shift_id=ctx["shift_id"],
        unit_price="80.00",
        payments=[{"payment_method": "cash", "amount": "80.00"}],
    )
    sale_ret = create_sale(
        client,
        ctx,
        headers=ctx["manager_headers"],
        shift_id=ctx["shift_id"],
        unit_price="60.00",
        payments=[{"payment_method": "cash", "amount": "60.00"}],
    )
    if sale_void.status_code != 201 or sale_ret.status_code != 201:
        record("reporting setup sales", False, "")
        return

    client.post(
        f"{BASE}/sales/{sale_void.json()['id']}/void",
        headers=ctx["manager_headers"],
    )
    ret_sale = sale_ret.json()
    client.post(
        f"{BASE}/returns",
        headers=ctx["manager_headers"],
        json={
            "branch_id": ctx["branch_id"],
            "sale_id": ret_sale["id"],
            "lines": [
                {
                    "product_id": ctx["product_id"],
                    "sale_line_id": ret_sale["lines"][0]["id"],
                    "qty": "1",
                    "unit_price": "60.00",
                }
            ],
            "refund_payments": [{"payment_method": "cash", "amount": "60.00"}],
        },
    )

    summary = client.get(
        f"{BASE}/analytics/sales-summary",
        headers=ctx["owner_headers"],
        params={"date_from": today, "date_to": today},
    )
    if summary.status_code != 200:
        record("sales summary fetch", False, str(summary.status_code))
        return
    total_returns = Decimal(str(summary.json()["total_returns"]))
    record(
        "total_returns counts genuine return only",
        total_returns == Decimal("60.00"),
        f"total_returns={total_returns}",
    )


def main() -> int:
    suffix = uuid.uuid4().int % 10_000_000
    with httpx.Client(timeout=60.0) as client:
        ctx = setup_tenant(client, suffix)
        test_void_cash_sale(client, ctx)
        test_void_credit_sale(client, ctx)
        test_void_blocked_closed_shift(client, ctx)
        test_void_blocked_with_returns(client, ctx)
        test_void_cashier_forbidden(client, ctx)
        test_partially_paid_cancel_reversal(client, ctx)
        test_draft_cancel_unchanged(client, ctx)
        test_reporting_void_vs_return(client, suffix)

    failed = [r for r in RESULTS if not r[1]]
    print("\n=== SALE VOID TEST SUMMARY ===")
    print(
        f"Total: {len(RESULTS)}, Passed: {len(RESULTS) - len(failed)}, "
        f"Failed: {len(failed)}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
