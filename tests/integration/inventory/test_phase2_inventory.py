"""Phase 2: branch-aware FIFO, adjustments/waste/transfers on layers, GRN rules."""

from __future__ import annotations

import pytest

from tests.helpers.db import db_rows, db_scalar
from tests.helpers.phase2 import (
    D,
    add_user,
    new_product,
    post_sale,
    second_branch,
    stock_in,
    stock_qty,
)
from tests.helpers.tenants import build_fifo_tenant

pytestmark = pytest.mark.integration


def _po(client, t, lines, branch_id=None):
    r = client.post(
        t.url("/purchases/orders"),
        headers=t.owner_headers,
        json={
            "supplier_id": t.supplier_id,
            "branch_id": branch_id or t.branch_id,
            "lines": [{"product_id": t.product_id, "ordered_qty": q, "cost_per_unit": c} for q, c in lines],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _grn(client, t, po, line, qty, cost, **extra):
    body = {
        "branch_id": t.branch_id,
        "supplier_id": t.supplier_id,
        "purchase_order_id": po["id"],
        "lines": [{"product_id": t.product_id, "purchase_line_id": line["id"], "qty_received": qty, "cost_per_unit": cost}],
    }
    body.update(extra)
    return client.post(t.url("/purchases/receipts"), headers=t.owner_headers, json=body)


@pytest.fixture
def fifo(http_client, api_base, unique_suffix):
    return build_fifo_tenant(http_client, api_base, unique_suffix)


def _layers(t, branch_id=None):
    return db_rows(
        """SELECT id, branch_id, cost_per_unit, qty_remaining, received_qty, layer_source
           FROM purchase_lines WHERE business_id = :b AND product_id = :p AND branch_id = :br
             AND deleted_at IS NULL ORDER BY created_at, id""",
        {"b": t.business_id, "p": t.product_id, "br": branch_id or t.branch_id},
    )


def test_fifo_order_ignores_updated_at_with_interleaved_partial_consumption(http_client, fifo):
    t = fifo
    po = _po(http_client, t, [("5", "10.00"), ("5", "20.00")])
    a, b = po["lines"]
    assert _grn(http_client, t, po, a, "5", "10.00").status_code == 201
    assert _grn(http_client, t, po, b, "5", "20.00").status_code == 201
    # sale 1 partially consumes layer A (bumping its updated_at past B's)
    assert post_sale(http_client, t, qty="3", unit_price="100").status_code == 201
    # sale 2 must finish A (2 @10) before touching B (2 @20)
    s2 = post_sale(http_client, t, qty="4", unit_price="100").json()
    assert D(s2["lines"][0]["cost_per_unit"]) == D("15.0000")
    moves = db_rows(
        "SELECT cost_per_unit, qty FROM stock_movements WHERE reference_id = :l AND movement_type = 'sale' ORDER BY movement_sequence",
        {"l": s2["lines"][0]["id"]},
    )
    assert [(D(m["cost_per_unit"]), D(m["qty"])) for m in moves] == [(D("10"), D("-2")), (D("20"), D("-2"))]
    assert [D(x["qty_remaining"]) for x in _layers(t)] == [D("0"), D("3")]


def test_layers_are_per_branch_and_waste_uses_them(http_client, fifo):
    t = fifo
    po = _po(http_client, t, [("10", "10.00")])
    assert _grn(http_client, t, po, po["lines"][0], "10", "10.00").status_code == 201
    other = second_branch(http_client, t)
    stock_in(http_client, t, t.product_id, "10", "70.00", branch_id=other)

    waste = http_client.post(
        t.url("/waste"), headers=t.owner_headers,
        json={"branch_id": other, "reason": "expiry",
              "lines": [{"product_id": t.product_id, "qty": "4", "cost_per_unit": "999"}]},
    )
    assert waste.status_code == 201, waste.text
    assert D(waste.json()["lines"][0]["cost_per_unit"]) == D("70")  # the other branch's layer, not 10, not 999
    assert [D(x["qty_remaining"]) for x in _layers(t)] == [D("10")]  # main branch untouched
    assert [D(x["qty_remaining"]) for x in _layers(t, other)] == [D("6")]
    assert stock_qty(t, branch_id=other) == D("6")


def test_adjustments_consume_and_create_layers_with_server_cost(http_client, fifo):
    t = fifo
    po = _po(http_client, t, [("10", "10.00")])
    assert _grn(http_client, t, po, po["lines"][0], "10", "10.00").status_code == 201
    adj = lambda delta, reason, cost: http_client.post(  # noqa: E731
        t.url("/adjustments"), headers=t.owner_headers,
        json={"branch_id": t.branch_id, "reason": reason,
              "lines": [{"product_id": t.product_id, "qty_delta": delta, "cost_per_unit": cost}]},
    )
    out = adj("-3", "damage", "999")
    assert out.status_code == 201, out.text
    assert D(out.json()["lines"][0]["cost_per_unit"]) == D("10")
    assert [D(x["qty_remaining"]) for x in _layers(t)] == [D("7")]

    gain = adj("2", "count_correction", "999")  # client cost ignored: last known cost
    assert gain.status_code == 201
    assert D(gain.json()["lines"][0]["cost_per_unit"]) == D("10")
    ob = adj("1", "opening_balance", "33.50")  # only case the client cost is used
    assert D(ob.json()["lines"][0]["cost_per_unit"]) == D("33.50")
    layers = _layers(t)
    assert [(x["layer_source"], D(x["qty_remaining"])) for x in layers][-2:] == [("adjustment", D("2")), ("adjustment", D("1"))]
    assert stock_qty(t) == D("10")  # 10 - 3 + 2 + 1 == sum of layers 7 + 2 + 1
    assert sum(D(x["qty_remaining"]) for x in layers) == D("10")


def test_transfer_is_in_transit_until_received_and_moves_cost(http_client, fifo):
    t = fifo
    po = _po(http_client, t, [("5", "10.00"), ("5", "20.00")])
    assert _grn(http_client, t, po, po["lines"][0], "5", "10.00").status_code == 201
    assert _grn(http_client, t, po, po["lines"][1], "5", "20.00").status_code == 201
    dest = second_branch(http_client, t)

    tr = http_client.post(
        t.url("/transfers"), headers=t.owner_headers,
        json={"source_branch_id": t.branch_id, "dest_branch_id": dest,
              "lines": [{"product_id": t.product_id, "qty": "7", "cost_per_unit": "1"}]},
    )
    assert tr.status_code == 201, tr.text
    body = tr.json()
    assert body["status"] == "in_transit"
    assert D(body["lines"][0]["cost_per_unit"]) == D("12.8571")  # (5*10 + 2*20) / 7
    assert stock_qty(t) == D("3")
    assert stock_qty(t, branch_id=dest) == D("0")  # not posted at the destination yet
    assert _layers(t, dest) == []

    rcv = http_client.post(t.url(f"/transfers/{body['id']}/receive"), headers=t.owner_headers)
    assert rcv.status_code == 200 and rcv.json()["status"] == "received"
    assert stock_qty(t, branch_id=dest) == D("7")
    dest_layers = _layers(t, dest)
    assert [(D(x["cost_per_unit"]), D(x["qty_remaining"])) for x in dest_layers] == [(D("10"), D("5")), (D("20"), D("2"))]
    assert http_client.post(t.url(f"/transfers/{body['id']}/receive"), headers=t.owner_headers).status_code == 400


def test_transfer_receiver_must_belong_to_destination_branch(http_client, fifo):
    t = fifo
    po = _po(http_client, t, [("5", "10.00")])
    assert _grn(http_client, t, po, po["lines"][0], "5", "10.00").status_code == 201
    dest = second_branch(http_client, t)
    tr = http_client.post(
        t.url("/transfers"), headers=t.owner_headers,
        json={"source_branch_id": t.branch_id, "dest_branch_id": dest,
              "lines": [{"product_id": t.product_id, "qty": "2"}]},
    ).json()
    source_only_manager = add_user(http_client, t, "manager", branch_id=t.branch_id)
    denied = http_client.post(t.url(f"/transfers/{tr['id']}/receive"), headers=source_only_manager)
    assert denied.status_code == 403
    dest_manager = add_user(http_client, t, "manager", branch_id=dest)
    assert http_client.post(t.url(f"/transfers/{tr['id']}/receive"), headers=dest_manager).status_code == 200


def test_transfer_needs_stock_and_distinct_branches(http_client, fifo):
    t = fifo
    dest = second_branch(http_client, t)
    r = http_client.post(
        t.url("/transfers"), headers=t.owner_headers,
        json={"source_branch_id": t.branch_id, "dest_branch_id": dest,
              "lines": [{"product_id": t.product_id, "qty": "1"}]},
    )
    assert r.status_code == 400 and "Insufficient" in r.json()["detail"]


# ── goods receipt ────────────────────────────────────────────────────────────


def test_grn_validations(http_client, fifo):
    t = fifo
    po = _po(http_client, t, [("5", "10.00")])
    other_po = _po(http_client, t, [("5", "10.00")])
    line = po["lines"][0]

    # purchase line of a different PO
    assert _grn(http_client, t, po, other_po["lines"][0], "1", "10.00").status_code == 400
    # product mismatch
    wrong = new_product(http_client, t)
    r = http_client.post(
        t.url("/purchases/receipts"), headers=t.owner_headers,
        json={"branch_id": t.branch_id, "supplier_id": t.supplier_id, "purchase_order_id": po["id"],
              "lines": [{"product_id": wrong, "purchase_line_id": line["id"], "qty_received": "1", "cost_per_unit": "10"}]},
    )
    assert r.status_code == 400
    # over-receipt
    assert _grn(http_client, t, po, line, "6", "10.00").status_code == 400
    assert _grn(http_client, t, po, line, "5", "10.00").status_code == 201
    assert _grn(http_client, t, po, line, "1", "10.00").status_code == 400
    # a purchase_line_id without a PO
    r = http_client.post(
        t.url("/purchases/receipts"), headers=t.owner_headers,
        json={"branch_id": t.branch_id, "supplier_id": t.supplier_id,
              "lines": [{"product_id": t.product_id, "purchase_line_id": line["id"], "qty_received": "1", "cost_per_unit": "10"}]},
    )
    assert r.status_code == 400
    # wrong branch / supplier for the PO
    assert _grn(http_client, t, po, line, "1", "10", branch_id=second_branch(http_client, t)).status_code == 400


def test_grn_always_creates_a_layer_and_posts_supplier_payable(http_client, fifo):
    t = fifo
    # standalone receipt (no PO line): still gets a layer
    r = http_client.post(
        t.url("/purchases/receipts"), headers=t.owner_headers,
        json={"branch_id": t.branch_id, "supplier_id": t.supplier_id,
              "lines": [{"product_id": t.product_id, "qty_received": "4", "cost_per_unit": "12.5"}]},
    )
    assert r.status_code == 201, r.text
    layers = _layers(t)
    assert len(layers) == 1 and layers[0]["layer_source"] == "receipt" and D(layers[0]["qty_remaining"]) == D("4")
    bal = db_scalar("SELECT COALESCE(SUM(amount),0) FROM supplier_ledger WHERE supplier_id = :s", {"s": t.supplier_id})
    assert D(bal) == D("-50.00")  # we owe 4 x 12.50

    # PO receipt at a different invoiced cost gets its own layer; PO line tracks receipts
    po = _po(http_client, t, [("10", "10.00")])
    assert _grn(http_client, t, po, po["lines"][0], "6", "11.00").status_code == 201
    assert _grn(http_client, t, po, po["lines"][0], "4", "10.00").status_code == 201
    costs = sorted((D(x["cost_per_unit"]), D(x["qty_remaining"])) for x in _layers(t))
    assert costs == [(D("10"), D("4")), (D("11"), D("6")), (D("12.5"), D("4"))]
    detail = http_client.get(t.url(f"/purchases/orders/{po['id']}"), headers=t.owner_headers).json()
    assert detail["status"] == "received" and D(detail["lines"][0]["received_qty"]) == D("10")
    bal = db_scalar("SELECT COALESCE(SUM(amount),0) FROM supplier_ledger WHERE supplier_id = :s", {"s": t.supplier_id})
    assert D(bal) == D("-50.00") - D("66.00") - D("40.00")


def test_unit_cost_keeps_four_decimals_end_to_end(http_client, fifo):
    t = fifo
    po = _po(http_client, t, [("3", "10.1234")])
    assert _grn(http_client, t, po, po["lines"][0], "3", "10.1234").status_code == 201
    sql_cost = D(db_scalar("SELECT get_fifo_cost(:b, :br, :p, NULL::uuid, 3)", {"b": t.business_id, "br": t.branch_id, "p": t.product_id}))
    sale = post_sale(http_client, t, qty="3", unit_price="20").json()
    assert D(sale["lines"][0]["cost_per_unit"]) == sql_cost == D("10.1234")
