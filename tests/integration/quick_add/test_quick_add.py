"""POST /products/quick-add."""

from __future__ import annotations

import time
import uuid

import pytest

from tests.helpers.db import db_rows, db_scalar
from tests.helpers.phase2 import D, add_user, second_branch
from tests.helpers.sync import snapshot_all
from tests.helpers.tenants import create_supplier as make_supplier
from tests.integration.quick_add.helpers import (
    default_price,
    ean13,
    layers,
    line,
    lookup,
    post_qa,
    quick_add,
    stock_of,
)
from tests.integration.tax.helpers import make_rate, put_setting

pytestmark = pytest.mark.integration


def _by_cid(res: dict) -> dict:
    return {ln["client_line_id"]: ln for ln in res["lines"]}


def _product(client, t, product_id: str) -> dict:
    r = client.get(t.url(f"/products/{product_id}"), headers=t.owner_headers)
    assert r.status_code == 200, r.text
    return r.json()


# ── mixed batch ──────────────────────────────────────────────────────────────


def test_mixed_batch_existing_new_and_failing(http_client, pos_tenant):
    t = pos_tenant
    old_code = ean13()
    pid_old = quick_add(
        http_client, t, [line(old_code, name="Old", qty="5", cost_price="30", sale_price="45")]
    )["lines"][0]["product_id"]

    res = quick_add(
        http_client,
        t,
        [
            line(old_code, cid="old", name="ignored", qty="3", cost_price="99", sale_price="999"),
            line(cid="new", name="Brand New", qty="8", cost_price="20", sale_price="30"),
            line(cid="noname", name="  "),
            line(cid="negqty", qty="-1"),
            line(cid="badcost", cost_price="abc"),
        ],
    )
    got = _by_cid(res)
    assert (res["created"], res["updated"], res["failed"]) == (1, 1, 3)
    assert got["old"]["status"] == "updated" and got["old"]["product_id"] == pid_old
    assert got["new"]["status"] == "created" and got["new"]["product_id"] and got["new"]["variation_id"]
    assert got["noname"]["error"]["code"] == "name_required"
    assert got["negqty"]["error"]["code"] == "invalid_qty"
    assert got["badcost"]["error"]["code"] == "invalid_cost_price"
    for cid in ("noname", "negqty", "badcost"):
        assert got[cid]["status"] == "error" and got[cid].get("product_id") is None
    # the existing product got stock but NO price change (update_existing_prices defaulted to false)
    assert stock_of(t, pid_old) == D("8")
    assert default_price(t, pid_old) == D("45")
    assert stock_of(t, got["new"]["product_id"]) == D("8")


def test_update_existing_prices_flag(http_client, pos_tenant):
    t = pos_tenant
    code = ean13()
    pid = quick_add(http_client, t, [line(code, qty="1", sale_price="45")])["lines"][0]["product_id"]
    res = quick_add(
        http_client, t, [line(code, qty="1", sale_price="60", cost_price="33", update_existing_prices=True)]
    )
    assert res["lines"][0]["status"] == "updated"
    assert default_price(t, pid) == D("60")
    lay = layers(t, pid)
    assert D(lay[-1]["cost_per_unit"]) == D("33")  # the layer always carries the line's own cost
    assert D(lookup(http_client, t, code).json()["product"]["cost_price"]) == D("33")


def test_duplicate_barcode_in_one_request_is_merged(http_client, pos_tenant):
    t = pos_tenant
    code = ean13()
    res = quick_add(
        http_client,
        t,
        [line(code, cid="a", name="Twin", qty="2"), line(code, cid="b", name="Twin again", qty="3")],
    )
    got = _by_cid(res)
    assert got["a"]["status"] == "created"
    assert got["b"]["status"] == "updated" and got["b"]["note"] == "duplicate_barcode_merged"
    assert got["a"]["product_id"] == got["b"]["product_id"]
    assert stock_of(t, got["a"]["product_id"]) == D("5")
    assert (
        db_scalar(
            "SELECT COUNT(*) FROM barcodes WHERE business_id = :b AND barcode = :c",
            {"b": t.business_id, "c": code},
        )
        == 1
    )


def test_same_item_spelled_as_upca_matches_existing(http_client, pos_tenant):
    t = pos_tenant
    code = ean13("036")
    pid = quick_add(http_client, t, [line(code, qty="1")])["lines"][0]["product_id"]
    res = quick_add(http_client, t, [line(code[1:], qty="4")])  # the 12-digit UPC-A spelling
    assert res["lines"][0]["status"] == "updated" and res["lines"][0]["product_id"] == pid
    assert stock_of(t, pid) == D("5")


# ── new product details ──────────────────────────────────────────────────────


def test_new_product_defaults_price_list_item_and_flags(http_client, rbac_tenant):
    t = rbac_tenant  # the fixture creates no unit: the "Piece" default must be provisioned
    code = ean13()
    res = quick_add(
        http_client,
        t,
        [
            line(
                code,
                name="Defaults",
                brand="Acme",
                pack_size="500g",
                mrp="99",
                track_expiry=True,
                expiry_date="2030-05-01",
                batch_no="B1",
            )
        ],
    )
    pid = res["lines"][0]["product_id"]
    p = _product(http_client, t, pid)
    assert p["name"] == "Defaults"
    assert p["category"] is None
    assert p["track_expiry"] is True
    assert p["pack_size"] == "500g" and D(p["mrp"]) == D("99")
    assert p["tax_rate_id"] is None and p["is_tax_exempt"] is False  # inherits the business default
    assert p["base_unit"]["name"] == "Piece"
    assert p["brand"]["name"] == "Acme"
    assert default_price(t, pid) == D("70")
    row = db_rows(
        "SELECT l.name, i.variation_id, i.min_qty FROM price_list_items i "
        "JOIN price_lists l ON l.id = i.price_list_id "
        "WHERE i.business_id = :b AND i.product_id = :p AND i.deleted_at IS NULL",
        {"b": t.business_id, "p": pid},
    )
    assert len(row) == 1 and row[0]["name"] == "Retail" and row[0]["variation_id"] is None
    assert (
        db_scalar("SELECT COUNT(*) FROM barcodes WHERE product_id = :p AND barcode = :c", {"p": pid, "c": code})
        == 1
    )
    assert (
        db_scalar("SELECT COUNT(*) FROM product_variations WHERE product_id = :p AND is_default", {"p": pid})
        == 1
    )
    # the next request reuses the unit and the brand
    res2 = quick_add(http_client, t, [line(name="Second", brand="acme")])
    p2 = _product(http_client, t, res2["lines"][0]["product_id"])
    assert p2["base_unit"]["id"] == p["base_unit"]["id"] and p2["brand"]["id"] == p["brand"]["id"]
    assert (
        db_scalar(
            "SELECT COUNT(*) FROM units WHERE business_id = :b AND lower(name) = 'piece'",
            {"b": t.business_id},
        )
        == 1
    )


def test_uses_the_business_piece_unit(http_client, pos_tenant):
    t = pos_tenant
    res = quick_add(http_client, t, [line()])
    assert _product(http_client, t, res["lines"][0]["product_id"])["base_unit"]["id"] == t.unit_id


def test_tax_comes_from_the_business_default_unless_given(http_client, pos_tenant):
    t = pos_tenant
    rate = make_rate(http_client, t, "17")
    res = quick_add(
        http_client,
        t,
        [
            line(cid="plain"),
            line(cid="rated", tax_rate_id=rate),
            line(cid="exempt", is_tax_exempt=True),
            line(cid="badrate", tax_rate_id=str(uuid.uuid4())),
        ],
    )
    got = _by_cid(res)
    plain = _product(http_client, t, got["plain"]["product_id"])
    assert plain["tax_rate_id"] is None and plain["is_tax_exempt"] is False
    assert _product(http_client, t, got["rated"]["product_id"])["tax_rate_id"] == rate
    assert _product(http_client, t, got["exempt"]["product_id"])["is_tax_exempt"] is True
    assert got["badrate"]["status"] == "error" and got["badrate"]["error"]["code"] == "invalid_tax_rate"


def test_zero_price_writes_no_price_item_and_qty_zero_adds_no_stock(http_client, pos_tenant):
    t = pos_tenant
    res = quick_add(http_client, t, [line(cid="z", qty="0", sale_price="0")])
    pid = res["lines"][0]["product_id"]
    assert res["lines"][0]["status"] == "created"
    assert default_price(t, pid) is None
    assert stock_of(t, pid) == 0
    assert res["adjustment_id"] is None and res["purchase_id"] is None  # nothing to post
    assert layers(t, pid) == []


def test_bad_references_and_formats_are_line_errors(http_client, pos_tenant):
    t = pos_tenant
    res = quick_add(
        http_client,
        t,
        [
            line(cid="cat", category_id=str(uuid.uuid4())),
            line(cid="unit", unit_id=str(uuid.uuid4())),
            line(cid="exp", expiry_date="not-a-date"),
            line(cid="bc", barcode="12"),
            line(cid="nobc", barcode=""),
            line(cid="ok"),
        ],
    )
    got = _by_cid(res)
    assert got["cat"]["error"]["code"] == "invalid_category"
    assert got["unit"]["error"]["code"] == "invalid_unit"
    assert got["exp"]["error"]["code"] == "invalid_expiry_date"
    assert got["bc"]["error"]["code"] == "invalid_barcode"
    assert got["nobc"]["error"]["code"] == "barcode_required"
    assert got["ok"]["status"] == "created"
    assert res["failed"] == 5


def test_category_and_unit_given(http_client, pos_tenant):
    t = pos_tenant
    cat = http_client.post(
        t.url("/categories"), headers=t.owner_headers, json={"name": f"C{uuid.uuid4().hex[:5]}"}
    ).json()["id"]
    unit = http_client.post(
        t.url("/units"),
        headers=t.owner_headers,
        json={"name": f"Pack{uuid.uuid4().hex[:4]}", "symbol": "pk", "is_base_unit": True},
    ).json()["id"]
    res = quick_add(http_client, t, [line(category_id=cat, unit_id=unit)])
    p = _product(http_client, t, res["lines"][0]["product_id"])
    assert p["category"]["id"] == cat and p["base_unit"]["id"] == unit


def test_soft_deleted_barcode_is_a_line_error(http_client, pos_tenant):
    t = pos_tenant
    code = ean13()
    pid = quick_add(http_client, t, [line(code, qty="0")])["lines"][0]["product_id"]
    assert http_client.delete(t.url(f"/products/{pid}"), headers=t.owner_headers).status_code == 200
    res = quick_add(http_client, t, [line(code, cid="x"), line(cid="y")])
    got = _by_cid(res)
    assert got["x"]["error"]["code"] == "barcode_deleted"
    assert got["y"]["status"] == "created"


# ── expiry / batch validation ────────────────────────────────────────────────


def test_track_expiry_requires_expiry_when_qty_positive(http_client, pos_tenant):
    t = pos_tenant
    res = quick_add(
        http_client,
        t,
        [
            line(cid="missing", track_expiry=True),
            line(cid="given", track_expiry=True, expiry_date="2031-02-01", batch_no="L7"),
            line(cid="nostock", track_expiry=True, qty="0"),
        ],
    )
    got = _by_cid(res)
    assert got["missing"]["error"]["code"] == "expiry_required"
    assert got["given"]["status"] == "created"
    assert got["nostock"]["status"] == "created"  # qty 0: nothing to expire
    lay = layers(t, got["given"]["product_id"])
    assert len(lay) == 1 and lay[0]["batch_number"] == "L7" and str(lay[0]["expiry_date"]) == "2031-02-01"


def test_existing_tracked_product_needs_expiry_too(http_client, pos_tenant):
    t = pos_tenant
    code = ean13()
    quick_add(http_client, t, [line(code, track_expiry=True, expiry_date="2031-02-01")])
    res = quick_add(http_client, t, [line(code)])  # no track_expiry on the line: the product decides
    assert res["lines"][0]["error"]["code"] == "expiry_required"
    ok = quick_add(http_client, t, [line(code, expiry_date="2032-01-01")])
    assert ok["lines"][0]["status"] == "updated"


def test_batch_required_setting(http_client, pos_tenant):
    t = pos_tenant
    assert put_setting(http_client, t, "inventory.require_batch", True).status_code == 200
    res = quick_add(http_client, t, [line(cid="nb"), line(cid="b", batch_no="B-1"), line(cid="zero", qty="0")])
    got = _by_cid(res)
    assert got["nb"]["error"]["code"] == "batch_required"
    assert got["b"]["status"] == "created"
    assert got["zero"]["status"] == "created"


# ── stock postings (existing services) ───────────────────────────────────────


def test_opening_stock_creates_one_adjustment_with_cost_layers(http_client, pos_tenant):
    t = pos_tenant
    a, b = ean13(), ean13()
    res = quick_add(
        http_client,
        t,
        [
            line(a, cid="a", qty="10", cost_price="12.5", batch_no="X1", track_expiry=True, expiry_date="2030-12-31"),
            line(b, cid="b", qty="4", cost_price="8"),
        ],
    )
    assert res["adjustment_id"] and res["purchase_id"] is None
    got = _by_cid(res)
    adj = db_rows(
        "SELECT reason, branch_id FROM stock_adjustments WHERE id = :i AND business_id = :b",
        {"i": res["adjustment_id"], "b": t.business_id},
    )
    assert len(adj) == 1 and adj[0]["reason"] == "opening_balance" and str(adj[0]["branch_id"]) == t.branch_id
    assert (
        db_scalar(
            "SELECT COUNT(*) FROM stock_adjustment_lines WHERE stock_adjustment_id = :i", {"i": res["adjustment_id"]}
        )
        == 2
    )
    la = layers(t, got["a"]["product_id"])
    assert len(la) == 1
    assert (D(la[0]["qty_remaining"]), D(la[0]["cost_per_unit"]), la[0]["batch_number"]) == (
        D("10"),
        D("12.5"),
        "X1",
    )
    assert str(la[0]["expiry_date"]) == "2030-12-31"
    assert D(layers(t, got["b"]["product_id"])[0]["cost_per_unit"]) == D("8")
    assert stock_of(t, got["a"]["product_id"]) == D("10")


def test_purchase_mode_creates_po_grn_layers_and_supplier_payable(http_client, pos_tenant):
    t = pos_tenant
    sup = make_supplier(http_client, t, name=f"Sup {uuid.uuid4().hex[:5]}")
    a, b = ean13(), ean13()
    res = quick_add(
        http_client,
        t,
        [
            line(a, cid="a", qty="10", cost_price="20", batch_no="P1"),
            line(b, cid="b", qty="5", cost_price="100", track_expiry=True, expiry_date="2030-01-01"),
            line(cid="zero", qty="0"),
        ],
        mode="purchase",
        supplier_id=sup,
    )
    assert res["purchase_id"] and res["receipt_id"] and res["adjustment_id"] is None
    got = _by_cid(res)
    po = db_rows(
        "SELECT supplier_id, branch_id FROM purchase_orders WHERE id = :i", {"i": res["purchase_id"]}
    )
    assert len(po) == 1 and str(po[0]["supplier_id"]) == sup
    assert (
        db_scalar("SELECT COUNT(*) FROM purchase_lines WHERE purchase_order_id = :i", {"i": res["purchase_id"]})
        == 2
    )
    grn = db_rows(
        "SELECT purchase_order_id, supplier_id FROM purchase_receipts WHERE id = :i", {"i": res["receipt_id"]}
    )
    assert len(grn) == 1 and str(grn[0]["purchase_order_id"]) == res["purchase_id"]
    assert (
        db_scalar(
            "SELECT COUNT(*) FROM purchase_receipt_lines WHERE purchase_receipt_id = :i", {"i": res["receipt_id"]}
        )
        == 2
    )
    la = layers(t, got["a"]["product_id"])
    assert len(la) == 1
    assert (D(la[0]["qty_remaining"]), D(la[0]["cost_per_unit"]), la[0]["batch_number"]) == (
        D("10"),
        D("20"),
        "P1",
    )
    assert str(layers(t, got["b"]["product_id"])[0]["expiry_date"]) == "2030-01-01"
    assert stock_of(t, got["a"]["product_id"]) == D("10")
    assert stock_of(t, got["zero"]["product_id"]) == 0
    # supplier payable: 10*20 + 5*100 = 700 (the ledger stores a payable as a negative amount)
    total = D(
        db_scalar(
            "SELECT COALESCE(SUM(amount), 0) FROM supplier_ledger WHERE supplier_id = :s AND deleted_at IS NULL",
            {"s": sup},
        )
    )
    assert abs(total) == D("700")


def test_purchase_mode_without_supplier_falls_back_to_opening_stock(http_client, pos_tenant):
    res = quick_add(http_client, pos_tenant, [line()], mode="purchase")
    assert res["adjustment_id"] and res["purchase_id"] is None


def test_unknown_supplier_rejects_the_request_without_writes(http_client, pos_tenant):
    t = pos_tenant
    code = ean13()
    r = post_qa(http_client, t, [line(code)], mode="purchase", supplier_id=str(uuid.uuid4()))
    assert r.status_code == 404, r.text
    assert lookup(http_client, t, code).json()["found"] is False


def test_unknown_branch_rejects_the_request(http_client, pos_tenant):
    r = post_qa(http_client, pos_tenant, [line()], branch_id=str(uuid.uuid4()))
    assert r.status_code == 400, r.text


# ── partial failure isolation / idempotency / limits ─────────────────────────


def test_failing_lines_do_not_affect_the_others_and_leave_nothing_behind(http_client, pos_tenant):
    t = pos_tenant
    codes = [ean13() for _ in range(4)]
    names = [f"Iso {uuid.uuid4().hex[:6]}" for _ in range(4)]
    res = quick_add(
        http_client,
        t,
        [
            line(codes[0], cid="0", name=names[0]),
            line(codes[1], cid="1", name=names[1], track_expiry=True),  # no expiry -> fails
            line(codes[2], cid="2", name=names[2], category_id=str(uuid.uuid4())),  # fails
            line(codes[3], cid="3", name=names[3]),
        ],
    )
    got = _by_cid(res)
    assert [got[c]["status"] for c in "0123"] == ["created", "error", "error", "created"]
    for i in (1, 2):
        assert lookup(http_client, t, codes[i]).json()["found"] is False
        assert (
            db_scalar(
                "SELECT COUNT(*) FROM products WHERE business_id = :b AND name = :n",
                {"b": t.business_id, "n": names[i]},
            )
            == 0
        )
    for i in (0, 3):
        assert D(lookup(http_client, t, codes[i]).json()["product"]["stock_on_hand"]) == 10


def test_idempotent_replay_returns_the_stored_response(http_client, pos_tenant):
    t = pos_tenant
    key = f"qa-{uuid.uuid4().hex}"
    body = [line(cid="r1", qty="6")]
    first = post_qa(http_client, t, body, key=key)
    assert first.status_code == 200
    second = post_qa(http_client, t, body, key=key)
    assert second.status_code == 200
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.json() == first.json()
    pid = first.json()["lines"][0]["product_id"]
    assert stock_of(t, pid) == D("6")  # applied once
    other = post_qa(http_client, t, [line(cid="r2")], key=key)  # same key, different body
    assert other.status_code == 409


def test_without_a_key_a_retry_adds_stock_again(http_client, pos_tenant):
    t = pos_tenant
    body = [line(ean13(), qty="2")]
    quick_add(http_client, t, body)
    r = quick_add(http_client, t, body)
    assert r["lines"][0]["status"] == "updated"
    assert stock_of(t, r["lines"][0]["product_id"]) == D("4")


def test_max_lines_limit(http_client, pos_tenant):
    from app.core.config import settings

    too_many = [line(cid=str(i), qty="0") for i in range(settings.quick_add_max_lines + 1)]
    assert post_qa(http_client, pos_tenant, too_many).status_code == 413


def test_request_shape_validation(http_client, pos_tenant):
    t = pos_tenant
    assert post_qa(http_client, t, []).status_code == 422
    bad_mode = http_client.post(
        t.url("/products/quick-add"), headers=t.owner_headers, json={"mode": "bogus", "lines": [line()]}
    )
    assert bad_mode.status_code == 422
    no_cid = line()
    del no_cid["client_line_id"]
    assert post_qa(http_client, t, [no_cid]).status_code == 422


def test_numeric_json_barcode_and_numbers_are_accepted(http_client, pos_tenant):
    res = quick_add(
        http_client, pos_tenant, [line(int(ean13("8964")), qty=3, cost_price=10.5, sale_price=15)]
    )
    assert res["lines"][0]["status"] == "created"


def test_performance_100_lines_under_3_seconds(http_client, pos_tenant):
    t = pos_tenant
    old = [line(cid=f"o{i}", qty="2") for i in range(20)]
    quick_add(http_client, t, old)  # 20 products exist already
    lines = [line(old[i]["barcode"], cid=f"e{i}", qty="1") for i in range(20)] + [
        line(cid=f"n{i}", brand=f"B{i % 5}", qty="3") for i in range(80)
    ]
    start = time.perf_counter()
    res = quick_add(http_client, t, lines)
    took = time.perf_counter() - start
    assert (res["created"], res["updated"], res["failed"]) == (80, 20, 0)
    assert took < 3.0, f"100 lines took {took:.2f}s"


# ── permissions / branch scope ───────────────────────────────────────────────


def test_cashier_is_forbidden(http_client, pos_tenant):
    t = pos_tenant
    code = ean13()
    assert post_qa(http_client, t, [line(code)], headers=t.cashier_headers).status_code == 403
    sup = make_supplier(http_client, t)
    r = post_qa(http_client, t, [line(code)], mode="purchase", supplier_id=sup, headers=t.cashier_headers)
    assert r.status_code == 403
    assert lookup(http_client, t, code).json()["found"] is False


def test_unauthenticated_is_rejected(http_client, pos_tenant):
    r = http_client.post(
        pos_tenant.url("/products/quick-add"), json={"mode": "opening_stock", "lines": [line()]}
    )
    assert r.status_code in (401, 403)


def test_branch_scoped_user_is_pinned_to_their_branch(http_client, pos_tenant):
    t = pos_tenant
    other = second_branch(http_client, t)
    headers = add_user(http_client, t, "manager", branch_id=other)
    assert post_qa(http_client, t, [line()], headers=headers, branch_id=t.branch_id).status_code == 403
    r = post_qa(http_client, t, [line(qty="3")], headers=headers, branch_id=None)
    assert r.status_code == 200, r.text
    pid = r.json()["lines"][0]["product_id"]
    assert stock_of(t, pid, other) == D("3")
    assert stock_of(t, pid, t.branch_id) == 0


def test_multi_branch_owner_must_name_the_branch(http_client, pos_tenant):
    t = pos_tenant
    second_branch(http_client, t)
    assert post_qa(http_client, t, [line()], branch_id=None).status_code == 400


# ── audit / sync ─────────────────────────────────────────────────────────────


def test_audit_row_has_actor_and_counts(http_client, pos_tenant):
    t = pos_tenant
    res = quick_add(http_client, t, [line(cid="a"), line(cid="b", name="")])
    rows = db_rows(
        "SELECT user_id, new_values FROM audit_logs WHERE business_id = :b AND record_id = :r "
        "AND new_values->>'event' = 'quick_add'",
        {"b": t.business_id, "r": res["quick_add_id"]},
    )
    assert len(rows) == 1
    nv = rows[0]["new_values"]
    assert (nv["created"], nv["updated"], nv["failed"], nv["lines"]) == (1, 0, 1, 2)
    assert nv["adjustment_id"] == res["adjustment_id"]
    me = http_client.get(t.url("/auth/me"), headers=t.owner_headers).json()
    assert str(rows[0]["user_id"]) == me["id"] and nv["actor"] == me["id"]


def test_created_records_arrive_in_sync(http_client, pos_tenant):
    t = pos_tenant
    code = ean13()
    res = quick_add(http_client, t, [line(code, name="Sync Me", sale_price="88")])
    pid = res["lines"][0]["product_id"]
    changes, _, _ = snapshot_all(http_client, t)
    by: dict[str, list] = {}
    for c in changes:
        by.setdefault(c["entity"], []).append(c)
    prods = [c for c in by.get("product", []) if c["id"] == pid]
    assert prods, sorted(by)
    assert "mrp" in prods[0]["data"] and "pack_size" in prods[0]["data"]
    assert any(c["data"].get("barcode") == code for c in by.get("barcode", [])), sorted(by)
    pli = by.get("price_list_item", [])
    assert any(
        c["data"].get("product_id") == pid and D(c["data"]["unit_price"]) == D("88") for c in pli
    ), sorted(by)
