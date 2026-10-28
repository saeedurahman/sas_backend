"""Phase 2: shift gate, payments/change, credit, tax, void permission, oversell locks."""

from __future__ import annotations

import concurrent.futures as cf

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

from tests.helpers.db import db_execute, db_rows, db_scalar
from tests.helpers.phase2 import (
    D,
    add_user,
    create_customer,
    customer_balance,
    new_product,
    open_shift_for,
    post_sale,
    second_branch,
    stock_in,
    stock_qty,
)

pytestmark = pytest.mark.integration


# ── shift gate ───────────────────────────────────────────────────────────────


def test_sale_requires_callers_own_open_shift(http_client, pos_tenant):
    t = pos_tenant
    # cashier has no shift
    r = post_sale(http_client, t, t.cashier_headers)
    assert r.status_code == 400 and "no open shift" in r.json()["detail"].lower()
    # ...and cannot borrow the owner's shift by id
    r = post_sale(http_client, t, t.cashier_headers, register_shift_id=t.shift_id)
    assert r.status_code == 403
    # the owner's own shift id is accepted, and is also derived when omitted
    ok = post_sale(http_client, t, register_shift_id=t.shift_id)
    assert ok.status_code == 201
    assert str(db_scalar("SELECT register_shift_id FROM sales WHERE id = :i", {"i": ok.json()["id"]})) == t.shift_id


def test_sale_rejected_when_shift_is_for_another_branch(http_client, pos_tenant):
    t = pos_tenant
    other = second_branch(http_client, t)
    body = {
        "branch_id": other,
        "lines": [{"product_id": t.product_id, "qty": "1", "unit_price": "100"}],
        "payments": [{"payment_method": "cash", "amount": "100"}],
    }
    r = http_client.post(t.url("/sales"), headers=t.owner_headers, json=body)
    assert r.status_code == 400 and "different branch" in r.json()["detail"]


def test_closing_someone_elses_shift_needs_owner_or_manage(http_client, pos_tenant):
    t = pos_tenant
    _, cashier_shift = open_shift_for(http_client, t, t.cashier_headers)
    oc = add_user(http_client, t, "cashier")
    denied = http_client.post(t.url(f"/shifts/{cashier_shift}/close"), headers=oc, json={"actual_cash": "0"})
    assert denied.status_code == 403
    assert (
        http_client.post(
            t.url(f"/shifts/{cashier_shift}/close"), headers=t.manager_headers, json={"actual_cash": "0"}
        ).status_code
        == 200
    )


def test_cash_movement_only_on_own_shift(http_client, pos_tenant):
    t = pos_tenant
    body = {"tx_type": "cash_in", "amount": "10.00"}
    foreign = http_client.post(t.url(f"/shifts/{t.shift_id}/cash-movement"), headers=t.manager_headers, json=body)
    assert foreign.status_code in (400, 403)
    own = http_client.post(t.url(f"/shifts/{t.shift_id}/cash-movement"), headers=t.owner_headers, json=body)
    assert own.status_code == 201


def test_unique_open_shift_indexes(http_client, pos_tenant):
    t = pos_tenant
    clone = """
        INSERT INTO register_shifts (id, business_id, branch_id, cash_register_id, opened_by,
            status, opening_float, opened_at, created_at, updated_at, created_by)
        SELECT gen_random_uuid(), business_id, branch_id, cash_register_id, opened_by,
            'open', 0, now(), now(), now(), created_by FROM register_shifts WHERE id = :id
    """
    with pytest.raises(IntegrityError):
        db_execute(clone, {"id": t.shift_id})


# ── payments / change ────────────────────────────────────────────────────────


def test_cash_overpayment_records_change_and_posts_net_cash(http_client, pos_tenant):
    t = pos_tenant
    before = D(http_client.get(t.url(f"/shifts/{t.shift_id}/summary"), headers=t.owner_headers).json()["expected_cash"])
    r = post_sale(http_client, t, unit_price="80.00", payments=[{"payment_method": "cash", "amount": "100.00"}])
    assert r.status_code == 201, r.text
    body = r.json()
    pay = body["payments"][0]
    assert (D(pay["amount"]), D(pay["tendered_amount"]), D(pay["change_given"])) == (D("80"), D("100"), D("20"))
    assert D(body["change_given"]) == D("20") and body["status"] == "completed"
    after = D(http_client.get(t.url(f"/shifts/{t.shift_id}/summary"), headers=t.owner_headers).json()["expected_cash"])
    assert after - before == D("80.00")


def test_unpaid_and_overpaid_non_cash_sales_rejected(http_client, pos_tenant):
    t = pos_tenant
    qty_before = stock_qty(t)
    assert post_sale(http_client, t, payments=[]).status_code == 400
    assert post_sale(http_client, t, payments=[{"payment_method": "card", "amount": "150"}]).status_code == 400
    assert stock_qty(t) == qty_before  # nothing consumed


def test_credit_ledger_uses_only_the_credit_portion_and_void_reverses_it(http_client, pos_tenant):
    t = pos_tenant
    cust = create_customer(http_client, t)
    r = post_sale(
        http_client, t, unit_price="100.00", customer_id=cust,
        payments=[{"payment_method": "cash", "amount": "30"}, {"payment_method": "credit", "amount": "70"}],
    )
    assert r.status_code == 201, r.text
    assert customer_balance(http_client, t, cust) == D("-70")
    void = http_client.post(t.url(f"/sales/{r.json()['id']}/void"), headers=t.owner_headers)
    assert void.status_code == 200, void.text
    assert customer_balance(http_client, t, cust) == D("0")
    entries = db_rows("SELECT entry_type, amount FROM customer_ledger WHERE customer_id = :c ORDER BY entry_at", {"c": cust})
    assert [(e["entry_type"], D(e["amount"])) for e in entries] == [("sale", D("-70")), ("refund", D("70"))]


def test_credit_limit_holds_under_concurrency(http_client, pos_tenant):
    t = pos_tenant
    cust = create_customer(http_client, t, credit_limit="100")
    _, mgr_shift = open_shift_for(http_client, t, t.manager_headers)

    def sell(headers):
        with httpx.Client(timeout=60) as c:
            return post_sale(
                c, t, headers, customer_id=cust, unit_price="80",
                payments=[{"payment_method": "credit", "amount": "80"}],
            ).status_code

    with cf.ThreadPoolExecutor(2) as pool:
        codes = sorted(pool.map(sell, [t.owner_headers, t.manager_headers]))
    assert codes == [201, 400]
    assert customer_balance(http_client, t, cust) == D("-80")


# ── stock locks ──────────────────────────────────────────────────────────────


def test_last_unit_cannot_be_oversold(http_client, pos_tenant):
    t = pos_tenant
    pid = new_product(http_client, t)
    stock_in(http_client, t, pid, "1")
    open_shift_for(http_client, t, t.manager_headers)

    def sell(headers):
        with httpx.Client(timeout=60) as c:
            return post_sale(c, t, headers, product_id=pid).status_code

    with cf.ThreadPoolExecutor(2) as pool:
        codes = sorted(pool.map(sell, [t.owner_headers, t.manager_headers]))
    assert codes == [201, 400]
    assert stock_qty(t, pid) == D("0")


def test_two_lines_of_same_product_are_checked_together(http_client, pos_tenant):
    t = pos_tenant  # 100 in stock
    body = {
        "branch_id": t.branch_id,
        "lines": [{"product_id": t.product_id, "qty": "60", "unit_price": "1"}] * 2,
        "payments": [{"payment_method": "cash", "amount": "120"}],
    }
    r = http_client.post(t.url("/sales"), headers=t.owner_headers, json=body)
    assert r.status_code == 400 and "Insufficient stock" in r.json()["detail"]


# ── tax ──────────────────────────────────────────────────────────────────────


def test_tax_is_resolved_server_side(http_client, pos_tenant):
    t = pos_tenant
    rate = http_client.post(t.url("/tax-rates"), headers=t.owner_headers, json={"name": "GST", "rate": "10"})
    assert rate.status_code == 201, rate.text
    db_execute("UPDATE products SET tax_rate_id = :r WHERE id = :p", {"r": rate.json()["id"], "p": t.product_id})
    open_shift_for(http_client, t, t.cashier_headers)

    # cashier has no sales.override_tax: the client's 0 is ignored
    cashier = post_sale(http_client, t, t.cashier_headers, payments=[{"payment_method": "cash", "amount": "110"}], line_extra={"tax_rate": "0"})
    assert cashier.status_code == 201, cashier.text
    line = cashier.json()["lines"][0]
    assert D(line["tax_rate"]) == D("10") and D(line["tax_amount"]) == D("10.00")

    # no client rate at all -> product rate
    plain = post_sale(http_client, t, payments=[{"payment_method": "cash", "amount": "110"}])
    assert D(plain.json()["lines"][0]["tax_amount"]) == D("10.00")

    # owner holds sales.override_tax and may override
    owner = post_sale(http_client, t, line_extra={"tax_rate": "0"})
    assert D(owner.json()["lines"][0]["tax_amount"]) == D("0")


def test_tax_rate_column_has_three_decimals(http_client, pos_tenant):
    t = pos_tenant
    r = http_client.post(t.url("/tax-rates"), headers=t.owner_headers, json={"name": "Odd", "rate": "12.375"})
    assert r.status_code == 201 and D(r.json()["rate"]) == D("12.375")


# ── void permission ──────────────────────────────────────────────────────────


def test_void_has_its_own_permission(http_client, pos_tenant):
    t = pos_tenant
    sale = post_sale(http_client, t).json()["id"]
    denied = http_client.post(t.url(f"/sales/{sale}/void"), headers=t.cashier_headers)
    assert denied.status_code == 403 and "sales.void" in denied.json()["detail"]
    assert http_client.post(t.url(f"/sales/{sale}/void"), headers=t.manager_headers).status_code == 200
    # the seed gave it to Owner and Manager only
    holders = db_rows(
        """SELECT DISTINCT lower(r.name) AS role FROM role_permissions rp
           JOIN permissions p ON p.id = rp.permission_id JOIN roles r ON r.id = rp.role_id
           WHERE p.permission_key = 'sales.void' AND rp.business_id = :b""",
        {"b": t.business_id},
    )
    assert {h["role"] for h in holders} == {"owner", "manager"}


# ── estimated cost ───────────────────────────────────────────────────────────


def test_cost_estimated_flag_and_fallback_cost(http_client, pos_tenant):
    t = pos_tenant
    # normal sale: layer-backed
    ok = post_sale(http_client, t).json()["lines"][0]
    assert ok["cost_estimated"] is False and D(ok["cost_per_unit"]) == D("50")
    # stock that has no layer (legacy movement), last known cost 50
    pid = new_product(http_client, t)
    stock_in(http_client, t, pid, "5", "42.00")
    db_execute("UPDATE purchase_lines SET qty_remaining = 0 WHERE product_id = :p", {"p": pid})  # layer exhausted
    est = post_sale(http_client, t, product_id=pid, qty="2").json()["lines"][0]
    assert est["cost_estimated"] is True
    assert D(est["cost_per_unit"]) == D("42.00")  # product's last cost, not 0
