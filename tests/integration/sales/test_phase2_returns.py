"""Phase 2: returns take money from the original sale line and cannot over-refund."""

from __future__ import annotations

import pytest

from tests.helpers.db import db_rows
from tests.helpers.phase2 import (
    D,
    create_customer,
    customer_balance,
    new_product,
    post_sale,
    stock_in,
    stock_qty,
)

pytestmark = pytest.mark.integration


def _return(client, t, sale, qty, *, refunds=None, headers=None, line_extra=None, **extra):
    line = {"sale_line_id": sale["lines"][0]["id"], "qty": qty}
    line.update(line_extra or {})
    body = {
        "branch_id": t.branch_id,
        "sale_id": sale["id"],
        "lines": [line],
        "refund_payments": refunds if refunds is not None else [],
    }
    body.update(extra)
    return client.post(t.url("/returns"), headers=headers or t.owner_headers, json=body)


def test_return_ignores_client_price_and_honours_discount(http_client, pos_tenant):
    t = pos_tenant
    sale = post_sale(
        http_client, t, qty="2", unit_price="100.00",
        payments=[{"payment_method": "cash", "amount": "180"}],
        line_extra={"discount_amount": "20"},
    )
    assert sale.status_code == 201, sale.text
    sale = sale.json()
    # client claims a 9999 unit price; the original 100 less its share of the discount counts
    r = _return(
        http_client, t, sale, "1",
        refunds=[{"payment_method": "cash", "amount": "90"}],
        line_extra={"unit_price": "9999", "tax_amount": "500"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert D(body["total_refund"]) == D("90.00")
    assert D(body["lines"][0]["unit_price"]) == D("100.00")
    assert D(body["lines"][0]["discount_amount"]) == D("10.00")
    # returning the rest takes the remainder, so the whole sale refunds exactly 180
    r2 = _return(http_client, t, sale, "1", refunds=[{"payment_method": "cash", "amount": "90"}])
    assert r2.status_code == 201 and D(r2.json()["total_refund"]) == D("90.00")


def test_refund_cannot_exceed_total_and_overreturn_rejected(http_client, pos_tenant):
    t = pos_tenant
    sale = post_sale(http_client, t, qty="2").json()
    too_much = _return(http_client, t, sale, "1", refunds=[{"payment_method": "cash", "amount": "150"}])
    assert too_much.status_code == 400 and "exceed" in too_much.json()["detail"]
    over = _return(http_client, t, sale, "3", refunds=[{"payment_method": "cash", "amount": "300"}])
    assert over.status_code == 400
    ok = _return(http_client, t, sale, "2", refunds=[{"payment_method": "cash", "amount": "200"}])
    assert ok.status_code == 201
    again = _return(http_client, t, sale, "1", refunds=[{"payment_method": "cash", "amount": "100"}])
    assert again.status_code == 400  # nothing returnable left


def test_line_must_belong_to_sale_and_product(http_client, pos_tenant):
    t = pos_tenant
    a = post_sale(http_client, t).json()
    b = post_sale(http_client, t).json()
    wrong_sale = http_client.post(
        t.url("/returns"), headers=t.owner_headers,
        json={"branch_id": t.branch_id, "sale_id": a["id"],
              "lines": [{"sale_line_id": b["lines"][0]["id"], "qty": "1"}],
              "refund_payments": [{"payment_method": "cash", "amount": "100"}]},
    )
    assert wrong_sale.status_code == 400 and "does not belong" in wrong_sale.json()["detail"]
    other_product = new_product(http_client, t)
    wrong_product = _return(http_client, t, a, "1", line_extra={"product_id": other_product})
    assert wrong_product.status_code == 400 and "product" in wrong_product.json()["detail"]


def test_no_double_refund_cash_xor_account_credit(http_client, pos_tenant):
    t = pos_tenant
    cust = create_customer(http_client, t)
    sale = post_sale(http_client, t, customer_id=cust).json()
    # refunded in cash in full -> the account is untouched
    cash = _return(http_client, t, sale, "1", refunds=[{"payment_method": "cash", "amount": "100"}])
    assert cash.status_code == 201 and D(cash.json()["credit_amount"]) == 0
    assert customer_balance(http_client, t, cust) == 0

    # no cash out -> the whole refund becomes account credit
    sale2 = post_sale(http_client, t, customer_id=cust).json()
    credit = _return(http_client, t, sale2, "1")
    assert credit.status_code == 201 and D(credit.json()["credit_amount"]) == D("100")
    assert customer_balance(http_client, t, cust) == D("100")

    # partly cash: only the rest is credited
    sale3 = post_sale(http_client, t, customer_id=cust).json()
    split = _return(http_client, t, sale3, "1", refunds=[{"payment_method": "cash", "amount": "40"}])
    assert split.status_code == 201
    assert customer_balance(http_client, t, cust) == D("160")


def test_credit_sale_return_reduces_the_debt(http_client, pos_tenant):
    t = pos_tenant
    cust = create_customer(http_client, t)
    sale = post_sale(http_client, t, customer_id=cust, payments=[{"payment_method": "credit", "amount": "100"}]).json()
    assert customer_balance(http_client, t, cust) == D("-100")
    r = _return(http_client, t, sale, "1")
    assert r.status_code == 201
    assert customer_balance(http_client, t, cust) == D("0")


def test_uncovered_refund_without_customer_rejected(http_client, pos_tenant):
    t = pos_tenant
    sale = post_sale(http_client, t).json()
    r = _return(http_client, t, sale, "1")  # no refund payments, no customer
    assert r.status_code == 400


def test_return_needs_callers_shift_and_restores_stock(http_client, pos_tenant):
    t = pos_tenant
    sale = post_sale(http_client, t).json()
    before = stock_qty(t)
    denied = _return(http_client, t, sale, "1", headers=t.manager_headers,
                     refunds=[{"payment_method": "cash", "amount": "100"}])
    assert denied.status_code == 400 and "no open shift" in denied.json()["detail"].lower()
    ok = _return(http_client, t, sale, "1", refunds=[{"payment_method": "cash", "amount": "100"}])
    assert ok.status_code == 201
    assert stock_qty(t) == before + 1
    tx = db_rows(
        "SELECT register_shift_id FROM register_transactions WHERE reference_id = :r",
        {"r": ok.json()["id"]},
    )
    assert {str(x["register_shift_id"]) for x in tx} == {t.shift_id}


def test_return_branch_must_match_sale_branch(http_client, pos_tenant):
    t = pos_tenant
    sale = post_sale(http_client, t).json()
    from tests.helpers.phase2 import second_branch

    other = second_branch(http_client, t)
    r = _return(http_client, t, sale, "1", refunds=[{"payment_method": "cash", "amount": "100"}], branch_id=other)
    assert r.status_code == 400
