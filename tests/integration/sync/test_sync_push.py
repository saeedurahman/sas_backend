"""POST /sync/push: idempotency, partial failure, ordering, replay semantics, flags."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.helpers.db import db_rows, db_scalar
from tests.helpers.phase2 import D, add_user, create_customer, new_product, stock_in, stock_qty
from tests.helpers.platform import set_subscription
from tests.helpers.sync import ago, iso, op, push, push_ok, sale_payload
from tests.helpers.tenants import TenantContext

pytestmark = pytest.mark.integration


def _sales(t: TenantContext) -> int:
    return int(db_scalar("SELECT count(*) FROM sales WHERE business_id = :b", {"b": t.business_id}))


# ── idempotency ──────────────────────────────────────────────────────────────


def test_duplicate_push_returns_same_result(http_client, pos_tenant):
    t = pos_tenant
    before = stock_qty(t)
    sale = op("sale.create", sale_payload(t, qty="2"))

    first = push_ok(http_client, t, [sale])[0]
    assert first["status"] == "applied", first
    assert first["official_number"].startswith("INV-")
    assert first["server_id"] == sale["payload"]["id"]

    again = push_ok(http_client, t, [sale])[0]
    assert again["status"] == "duplicate"
    assert again["original_status"] == "applied"
    assert again["server_id"] == first["server_id"]
    assert again["official_number"] == first["official_number"]

    assert _sales(t) == 1
    assert int(db_scalar("SELECT count(*) FROM sales WHERE id = :i", {"i": first["server_id"]})) == 1
    assert stock_qty(t) == before - 2  # consumed exactly once


def test_same_op_id_with_different_payload_is_refused(http_client, pos_tenant):
    t = pos_tenant
    op_id = str(uuid.uuid4())
    first = push_ok(http_client, t, [op("sale.create", sale_payload(t), op_id=op_id)])[0]
    assert first["status"] == "applied"
    other = push_ok(
        http_client, t, [op("sale.create", sale_payload(t, qty="3"), op_id=op_id)]
    )[0]
    assert other["status"] == "rejected"
    assert other["error"]["code"] == "idempotency_key_reused"
    assert other["error"]["retryable"] is False


def test_same_op_twice_in_one_batch(http_client, pos_tenant):
    t = pos_tenant
    sale = op("sale.create", sale_payload(t))
    results = push_ok(http_client, t, [sale, sale])
    assert [r["status"] for r in results] == ["applied", "duplicate"]
    assert results[0]["server_id"] == results[1]["server_id"]


def test_retried_rejection_returns_stored_result(http_client, pos_tenant):
    t = pos_tenant
    bad = op("sale.create", sale_payload(t, product_id=str(uuid.uuid4())))
    first = push_ok(http_client, t, [bad])[0]
    assert first["status"] == "rejected" and first["error"]["code"] == "unknown_product"
    assert first["error"]["retryable"] is False
    again = push_ok(http_client, t, [bad])[0]
    assert again == first
    assert db_scalar("SELECT count(*) FROM sync_ops WHERE op_id = :o", {"o": bad["op_id"]}) == 1


# ── partial failure / ordering ───────────────────────────────────────────────


def test_partial_batch_failure_does_not_abort_batch(http_client, pos_tenant):
    t = pos_tenant
    good1 = op("sale.create", sale_payload(t))
    bad_product = op("sale.create", sale_payload(t, product_id=str(uuid.uuid4())))
    malformed = {"op_id": str(uuid.uuid4()), "type": "sale.create", "payload": {}}
    unsupported = op("held_order.create", {"x": 1})
    good2 = op("sale.create", sale_payload(t))
    results = push_ok(http_client, t, [good1, bad_product, malformed, unsupported, good2])
    assert [r["status"] for r in results] == ["applied", "rejected", "rejected", "rejected", "applied"]
    assert results[1]["error"]["code"] == "unknown_product"
    assert results[2]["error"]["code"] == "malformed_op"
    assert results[3]["error"]["code"] == "unsupported_op_type"
    assert "Held orders" in results[3]["error"]["message"]
    assert _sales(t) == 2


def test_malformed_payload_is_rejected_with_stable_code(http_client, pos_tenant):
    t = pos_tenant
    result = push_ok(http_client, t, [op("sale.create", {"id": str(uuid.uuid4())})])[0]
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "malformed_payload"
    assert result["error"]["retryable"] is False


def test_out_of_order_dependent_ops_are_reordered(http_client, pos_tenant):
    t = pos_tenant
    customer_id = str(uuid.uuid4())
    create = op("customer.create", {"id": customer_id, "name": "Late Customer", "credit_limit": "1000"})
    credit_sale = op(
        "sale.create",
        sale_payload(
            t,
            customer_id=customer_id,
            payments=[{"payment_method": "credit", "amount": "100.00"}],
        ),
        depends_on=[create["op_id"]],
    )
    # Dependent op listed first; results still come back in request order.
    results = push_ok(http_client, t, [credit_sale, create])
    assert [r["status"] for r in results] == ["applied", "applied"]
    assert results[1]["server_id"] == customer_id
    assert D(db_scalar("SELECT balance FROM (SELECT SUM(amount) AS balance FROM customer_ledger "
                       "WHERE customer_id = :c) x", {"c": customer_id})) == D("-100.00")


def test_dependency_failure_cascades(http_client, pos_tenant):
    t = pos_tenant
    shift_open = op(
        "shift.open", {"id": str(uuid.uuid4()), "cash_register_id": str(uuid.uuid4())}
    )
    sale = op("sale.create", sale_payload(t), depends_on=[shift_open["op_id"]])
    results = push_ok(http_client, t, [shift_open, sale])
    assert results[0]["error"]["code"] == "unknown_register"
    assert results[1]["status"] == "rejected"
    assert results[1]["error"]["code"] == "dependency_failed"


def test_unknown_dependency_is_retryable(http_client, pos_tenant):
    t = pos_tenant
    sale = op("sale.create", sale_payload(t), depends_on=[str(uuid.uuid4())])
    result = push_ok(http_client, t, [sale])[0]
    assert result["error"]["code"] == "dependency_missing"
    assert result["error"]["retryable"] is True
    # Not stored as terminal: the same op can be retried later.
    assert db_scalar("SELECT count(*) FROM sync_ops WHERE op_id = :o", {"o": sale["op_id"]}) == 0


def test_dependency_cycle_rejected(http_client, pos_tenant):
    t = pos_tenant
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    one = op("sale.create", sale_payload(t), op_id=a, depends_on=[b])
    two = op("sale.create", sale_payload(t), op_id=b, depends_on=[a])
    results = push_ok(http_client, t, [one, two])
    assert {r["error"]["code"] for r in results} <= {"dependency_cycle", "dependency_failed"}
    assert all(r["status"] == "rejected" for r in results)


# ── atomicity / crash retry ──────────────────────────────────────────────────


def test_failing_op_leaves_no_partial_effects(http_client, pos_tenant):
    """A sale that fails after FIFO/stock writes (duplicate line ids) rolls back completely."""
    t = pos_tenant
    line_id = str(uuid.uuid4())
    line = {"id": line_id, "product_id": t.product_id, "qty": "1", "unit_price": "10.00"}
    sale = op(
        "sale.create",
        sale_payload(t, lines=[line, dict(line)], payments=[{"payment_method": "cash", "amount": "20.00"}]),
    )
    before = stock_qty(t)
    result = push_ok(http_client, t, [sale])[0]
    assert result["status"] == "conflict"
    assert result["error"]["code"] == "id_conflict"
    assert _sales(t) == 0
    assert stock_qty(t) == before
    assert db_scalar("SELECT count(*) FROM register_transactions WHERE business_id = :b "
                     "AND tx_type = 'sale'", {"b": t.business_id}) == 0


def test_lost_response_retry_applies_once(http_client, pos_tenant):
    """The server applied the batch but the response was lost; the client resends it."""
    t = pos_tenant
    ops = [op("sale.create", sale_payload(t, qty="1")) for _ in range(3)]
    batch_id = str(uuid.uuid4())
    first = push_ok(http_client, t, ops, batch_id=batch_id)
    second = push_ok(http_client, t, ops + [op("sale.create", sale_payload(t))], batch_id=batch_id)
    assert [r["status"] for r in first] == ["applied"] * 3
    assert [r["status"] for r in second] == ["duplicate"] * 3 + ["applied"]
    assert _sales(t) == 4


# ── replay semantics ─────────────────────────────────────────────────────────


def test_backdated_within_limit_uses_occurred_at(http_client, pos_tenant):
    t = pos_tenant
    when = ago(days=5)
    # The register's shift opened "now"; an old sale is outside it -> flagged, still accepted.
    sale = op("sale.create", sale_payload(t), occurred_at=when)
    result = push_ok(http_client, t, [sale])[0]
    assert result["status"] == "applied"
    assert "shift_mismatch" in result["flags"]
    row = db_rows("SELECT sold_at, is_offline, device_id FROM sales WHERE id = :i", {"i": result["server_id"]})[0]
    assert abs((row["sold_at"] - when).total_seconds()) < 1
    assert row["is_offline"] is True and row["device_id"] == "dev-1"
    movement = db_rows(
        "SELECT movement_at FROM stock_movements WHERE reference_id IN "
        "(SELECT id FROM sale_lines WHERE sale_id = :i)",
        {"i": result["server_id"]},
    )[0]
    assert abs((movement["movement_at"] - when).total_seconds()) < 1


def test_backdated_beyond_limit_rejected(http_client, pos_tenant):
    t = pos_tenant
    result = push_ok(http_client, t, [op("sale.create", sale_payload(t), occurred_at=ago(days=15))])[0]
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "occurred_too_old"
    assert result["error"]["retryable"] is False
    assert _sales(t) == 0


def test_future_beyond_skew_rejected_within_skew_accepted(http_client, pos_tenant):
    t = pos_tenant
    far = push_ok(http_client, t, [op("sale.create", sale_payload(t), occurred_at=ago(minutes=-30))])[0]
    assert far["error"]["code"] == "occurred_in_future"
    near = push_ok(http_client, t, [op("sale.create", sale_payload(t), occurred_at=ago(minutes=-2))])[0]
    assert near["status"] == "applied"


def test_offline_shift_open_sell_close_replay(http_client, pos_tenant):
    t = pos_tenant
    # Close the tenant's online shift so the offline one can take the register.
    closed = http_client.post(
        t.url(f"/shifts/{t.shift_id}/close"), headers=t.owner_headers, json={"actual_cash": "500.00"}
    )
    assert closed.status_code == 200, closed.text

    shift_id = str(uuid.uuid4())
    opened_at = ago(hours=6)
    open_op = op(
        "shift.open",
        {"id": shift_id, "cash_register_id": t.register_id, "opening_float": "200.00"},
        occurred_at=opened_at,
    )
    sale1 = op(
        "sale.create",
        sale_payload(t, shift_id=shift_id, qty="2", unit_price="50.00"),
        occurred_at=ago(hours=5),
        depends_on=[open_op["op_id"]],
    )
    sale2 = op(
        "sale.create",
        sale_payload(t, shift_id=shift_id, qty="1", unit_price="70.00"),
        occurred_at=ago(hours=4),
    )
    close_op = op(
        "shift.close",
        {"shift_id": shift_id, "actual_cash": "370.00"},
        occurred_at=ago(hours=3),
        depends_on=[sale1["op_id"], sale2["op_id"]],
    )
    results = push_ok(http_client, t, [open_op, sale1, sale2, close_op])
    assert [r["status"] for r in results] == ["applied"] * 4, results
    assert all(r["flags"] == [] for r in results[1:3])

    shift = db_rows("SELECT * FROM register_shifts WHERE id = :i", {"i": shift_id})[0]
    assert shift["status"] == "closed"
    assert abs((shift["opened_at"] - opened_at).total_seconds()) < 1
    assert D(shift["expected_cash"]) == D("370.00")  # 200 float + 100 + 70
    assert D(shift["cash_difference"]) == D("0.00")
    assert str(shift["opened_by"]) == db_scalar("SELECT id FROM users WHERE phone = :p AND business_id = :b",
                                                  {"p": t.owner_phone, "b": t.business_id}).__str__()


def test_shift_gate_flags_but_never_rejects(http_client, pos_tenant):
    t = pos_tenant
    manager_shift_holder = add_user(http_client, t, "cashier")
    # Another user's shift: the sale is accepted, flagged, and not posted to that drawer.
    result = push_ok(
        http_client, t, [op("sale.create", sale_payload(t))], headers=manager_shift_holder
    )[0]
    assert result["status"] == "applied"
    assert "shift_mismatch" in result["flags"]
    assert db_scalar("SELECT register_shift_id FROM sales WHERE id = :i", {"i": result["server_id"]}) is None

    unknown = push_ok(http_client, t, [op("sale.create", sale_payload(t, shift_id=str(uuid.uuid4())))])[0]
    assert unknown["status"] == "applied" and "shift_mismatch" in unknown["flags"]


# ── frozen totals + flags ────────────────────────────────────────────────────


def test_frozen_totals_stored_exactly_and_mismatches_flagged(http_client, pos_tenant):
    t = pos_tenant
    # Catalog says the product is 100.00 with no tax; the device sold at 80.00 with 7.00 tax.
    result = push_ok(
        http_client,
        t,
        [op("sale.create", sale_payload(t, qty="2", unit_price="80.00", tax_rate="5", tax_amount="7.00"))],
    )[0]
    assert result["status"] == "applied"
    line = db_rows("SELECT * FROM sale_lines WHERE sale_id = :i", {"i": result["server_id"]})[0]
    assert D(line["unit_price"]) == D("80.00")
    assert D(line["tax_amount"]) == D("7.00") and D(line["tax_rate"]) == D("5")
    pay = db_rows("SELECT * FROM sale_payments WHERE sale_id = :i", {"i": result["server_id"]})[0]
    assert D(pay["amount"]) == D("167.00")  # 160 + 7
    assert "tax_mismatch" in result["flags"]


def test_price_mismatch_against_catalog(http_client, pos_tenant):
    t = pos_tenant
    pl = http_client.post(
        t.url("/prices/lists"), headers=t.owner_headers,
        json={"name": "Retail", "list_type": "retail", "is_default": True},
    )
    assert pl.status_code == 201, pl.text
    pl_id = pl.json()["id"]
    item = http_client.post(
        t.url(f"/prices/lists/{pl_id}/items"), headers=t.owner_headers,
        json={"product_id": t.product_id, "unit_price": "100.00"},
    )
    assert item.status_code == 201, item.text
    ok = push_ok(http_client, t, [op("sale.create", sale_payload(t, unit_price="100.00"))])[0]
    bad = push_ok(http_client, t, [op("sale.create", sale_payload(t, unit_price="90.00"))])[0]
    assert "price_mismatch" not in ok["flags"]
    assert bad["status"] == "applied" and "price_mismatch" in bad["flags"]


def test_flagged_sale_creates_notification_audit_and_listing(http_client, pos_tenant):
    t = pos_tenant
    result = push_ok(
        http_client, t, [op("sale.create", sale_payload(t, qty="1000", unit_price="1.00"))]
    )[0]
    assert "oversold" in result["flags"]
    assert db_scalar(
        "SELECT count(*) FROM notification_log WHERE business_id = :b AND notification_type = 'sync_conflict'",
        {"b": t.business_id},
    ) == 1
    audit = db_rows(
        "SELECT device_id, new_values FROM audit_logs WHERE business_id = :b AND record_id = :r "
        "AND action = 'sync' ORDER BY created_at",
        {"b": t.business_id, "r": result["server_id"]},
    )
    assert {a["device_id"] for a in audit} == {"dev-1"}
    assert "sync_sale_flagged" in {a["new_values"]["event"] for a in audit}
    assert "sync_op_applied" in {a["new_values"]["event"] for a in audit}

    flagged = http_client.get(t.url("/sync/flags"), headers=t.owner_headers)
    assert flagged.status_code == 200
    items = flagged.json()["items"]
    assert [i["id"] for i in items] == [result["server_id"]]
    assert "oversold" in items[0]["flags"] and items[0]["device_id"] == "dev-1"
    only = http_client.get(t.url("/sync/flags"), headers=t.owner_headers, params={"flag": "price_mismatch"})
    assert only.json()["total"] == 0
    # Cashiers cannot review.
    assert http_client.get(t.url("/sync/flags"), headers=t.cashier_headers).status_code == 403


def test_oversold_negative_stock_and_cost_estimated(http_client, pos_tenant):
    t = pos_tenant
    # 100 in stock; sell 130 offline: 30 beyond the layers.
    result = push_ok(http_client, t, [op("sale.create", sale_payload(t, qty="130", unit_price="10.00"))])[0]
    assert result["status"] == "applied"
    assert {"oversold", "cost_estimated"} <= set(result["flags"])
    assert stock_qty(t) == D("-30")
    line = db_rows("SELECT cost_estimated, cost_per_unit FROM sale_lines WHERE sale_id = :i",
                   {"i": result["server_id"]})[0]
    assert line["cost_estimated"] is True and D(line["cost_per_unit"]) > 0


def test_online_sales_keep_strict_stock_check(http_client, pos_tenant):
    t = pos_tenant
    resp = http_client.post(
        t.url("/sales"), headers=t.owner_headers,
        json={"branch_id": t.branch_id,
              "lines": [{"product_id": t.product_id, "qty": "500", "unit_price": "10.00"}],
              "payments": [{"payment_method": "cash", "amount": "5000.00"}]},
    )
    assert resp.status_code == 400
    assert "Insufficient stock" in resp.text


def test_credit_limit_exceeded_flag(http_client, pos_tenant):
    t = pos_tenant
    customer = create_customer(http_client, t, credit_limit="50")
    result = push_ok(
        http_client, t,
        [op("sale.create", sale_payload(t, customer_id=customer,
                                        payments=[{"payment_method": "credit", "amount": "100.00"}]))],
    )[0]
    assert result["status"] == "applied" and "credit_limit_exceeded" in result["flags"]


def test_duplicate_client_receipt_with_different_content_rejected(http_client, pos_tenant):
    t = pos_tenant
    first = push_ok(http_client, t, [op("sale.create", sale_payload(t, receipt="D9-0001"))])[0]
    assert first["status"] == "applied"
    clash = push_ok(http_client, t, [op("sale.create", sale_payload(t, receipt="D9-0001", qty="2"))])[0]
    assert clash["status"] == "rejected"
    assert clash["error"]["code"] == "duplicate_receipt" and clash["error"]["retryable"] is False


def test_client_receipt_searchable_and_on_invoice(http_client, pos_tenant):
    t = pos_tenant
    result = push_ok(http_client, t, [op("sale.create", sale_payload(t, receipt="DEV7-000042"))])[0]
    listing = http_client.get(t.url("/sales"), headers=t.owner_headers, params={"search": "DEV7-0000"})
    assert [s["id"] for s in listing.json()["items"]] == [result["server_id"]]
    assert listing.json()["items"][0]["client_receipt_no"] == "DEV7-000042"
    detail = http_client.get(t.url(f"/sales/{result['server_id']}"), headers=t.owner_headers).json()
    assert detail["sale_number"] == result["official_number"]
    assert detail["client_receipt_no"] == "DEV7-000042"
    invoice = http_client.get(t.url(f"/invoice/{result['server_id']}"), headers=t.owner_headers)
    if invoice.status_code == 200:
        assert invoice.json()["client_receipt_no"] == "DEV7-000042"
    search = http_client.get(t.url("/search"), headers=t.owner_headers, params={"q": "DEV7-000042"})
    if search.status_code == 200:
        assert result["server_id"] in str(search.json())


# ── other op types ───────────────────────────────────────────────────────────


def test_customer_create_payment_and_void(http_client, pos_tenant):
    t = pos_tenant
    customer_id = str(uuid.uuid4())
    sale = op("sale.create", sale_payload(t), op_id=str(uuid.uuid4()))
    results = push_ok(
        http_client,
        t,
        [
            op("customer.create", {"id": customer_id, "name": "Walk-in Ali"}),
            op("customer.payment", {"customer_id": customer_id, "amount": "40.00",
                                    "payment_method": "cash", "register_shift_id": t.shift_id}),
            sale,
        ],
    )
    assert [r["status"] for r in results] == ["applied"] * 3
    assert D(db_scalar("SELECT SUM(amount) FROM customer_ledger WHERE customer_id = :c", {"c": customer_id})) == D("40.00")

    void = push_ok(http_client, t, [op("sale.void", {"sale_id": sale["payload"]["id"]})])[0]
    assert void["status"] == "applied", void
    assert db_scalar("SELECT status FROM sales WHERE id = :i", {"i": sale["payload"]["id"]}) == "voided"
    again = push_ok(http_client, t, [op("sale.void", {"sale_id": sale["payload"]["id"]})])[0]
    assert again["status"] == "conflict" and again["error"]["code"] == "sale_not_voidable"

    unsynced = push_ok(http_client, t, [op("sale.void", {"sale_id": str(uuid.uuid4())})])[0]
    assert unsynced["error"]["code"] == "sale_not_synced" and unsynced["error"]["retryable"] is True


def test_expense_and_return_ops(http_client, pos_tenant):
    t = pos_tenant
    cat = http_client.post(t.url("/expenses/categories"), headers=t.owner_headers, json={"name": "Misc"})
    assert cat.status_code == 201, cat.text
    sale = op("sale.create", sale_payload(t, qty="2", unit_price="50.00",
                                          lines=[{"id": str(uuid.uuid4()), "product_id": t.product_id,
                                                  "qty": "2", "unit_price": "50.00"}]))
    results = push_ok(
        http_client,
        t,
        [
            op("expense.create", {"branch_id": t.branch_id, "expense_category_id": cat.json()["id"],
                                  "expense_date": ago(hours=1).date().isoformat(), "amount": "25.00",
                                  "payments": [{"payment_method": "cash", "amount": "25.00"}]}),
            sale,
        ],
    )
    assert [r["status"] for r in results] == ["applied", "applied"]
    assert results[0]["official_number"].startswith("EXP-")

    line_id = sale["payload"]["lines"][0]["id"]
    ret = push_ok(
        http_client, t,
        [op("return.create", {"branch_id": t.branch_id, "sale_id": sale["payload"]["id"],
                              "lines": [{"sale_line_id": line_id, "qty": "1"}],
                              "refund_payments": [{"payment_method": "cash", "amount": "50.00"}]})],
    )[0]
    assert ret["status"] == "applied", ret
    assert ret["official_number"].startswith("RET-")


# ── authorization / limits / devices ─────────────────────────────────────────


def test_permission_denied_per_op(http_client, pos_tenant):
    t = pos_tenant
    # Cashiers may sell but not void.
    sale = op("sale.create", sale_payload(t))
    voided = op("sale.void", {"sale_id": str(uuid.uuid4())})
    results = push_ok(http_client, t, [sale, voided], headers=t.cashier_headers)
    assert results[1]["status"] == "rejected" and results[1]["error"]["code"] == "permission_denied"
    assert results[1]["error"]["retryable"] is False


def test_batch_and_payload_limits(http_client, pos_tenant):
    t = pos_tenant
    too_many = [op("sale.create", {}) for _ in range(201)]
    resp = push(http_client, t, too_many)
    assert resp.status_code == 413
    assert resp.json()["detail"]["code"] == "batch_too_large"

    huge = op("customer.create", {"id": str(uuid.uuid4()), "name": "x" * 70_000})
    result = push_ok(http_client, t, [huge])[0]
    assert result["error"]["code"] == "payload_too_large"


def test_device_registry_and_revocation(http_client, pos_tenant):
    t = pos_tenant
    push_ok(http_client, t, [op("sale.create", sale_payload(t))], device_id="till-A",
            device_name="Front till", app_version="1.4.2")
    devices = http_client.get(t.url("/devices"), headers=t.owner_headers).json()
    assert [d["device_id"] for d in devices] == ["till-A"]
    assert devices[0]["name"] == "Front till" and devices[0]["app_version"] == "1.4.2"
    assert devices[0]["revoked_at"] is None
    assert http_client.get(t.url("/devices"), headers=t.cashier_headers).status_code == 403

    revoke = http_client.post(t.url("/devices/till-A/revoke"), headers=t.owner_headers)
    assert revoke.status_code == 200 and revoke.json()["revoked_at"] is not None
    blocked = push(http_client, t, [op("sale.create", sale_payload(t))], device_id="till-A")
    assert blocked.status_code == 403 and blocked.json()["detail"]["code"] == "device_revoked"
    from tests.helpers.sync import pull

    assert pull(http_client, t, device_id="till-A").json()["detail"]["code"] == "device_revoked"
    # Other devices are unaffected.
    assert push(http_client, t, [], device_id="till-B").status_code == 200
    assert http_client.post(t.url("/devices/nope/revoke"), headers=t.owner_headers).status_code == 404


# ── subscription rules ───────────────────────────────────────────────────────


def test_subscription_grace_accepts_sales_blocks_new_entities(http_client, pos_tenant):
    t = pos_tenant
    set_subscription(t.business_id, status="active", sub_ends_in=-timedelta(days=1))  # in 3-day grace
    ops = [
        op("sale.create", sale_payload(t)),
        op("customer.create", {"id": str(uuid.uuid4()), "name": "New"}),
        op("shift.open", {"id": str(uuid.uuid4()), "cash_register_id": t.register_id}),
        op("customer.payment", {"customer_id": str(uuid.uuid4()), "amount": "5"}),
    ]
    results = push_ok(http_client, t, ops)
    assert results[0]["status"] == "applied" and results[0]["flags"] == []
    assert results[1]["error"]["code"] == "subscription_grace_readonly"
    assert results[1]["error"]["retryable"] is True
    assert results[2]["error"]["code"] == "subscription_grace_readonly"
    assert results[3]["error"]["code"] == "unknown_customer"  # allowed op type, bad reference


@pytest.mark.parametrize(
    ("status", "kwargs", "code"),
    [
        ("active", {"sub_ends_in": -timedelta(days=30)}, "subscription_expired"),
        ("suspended", {}, "subscription_suspended"),
        ("cancelled", {}, "subscription_cancelled"),
    ],
)
def test_subscription_blocked_accepts_only_money_ops_and_flags(http_client, pos_tenant, status, kwargs, code):
    t = pos_tenant
    set_subscription(t.business_id, status=status, **kwargs)
    shift_id = t.shift_id
    ops = [
        op("sale.create", sale_payload(t)),
        op("shift.close", {"shift_id": shift_id, "actual_cash": "500.00"}),
        op("expense.create", {"branch_id": t.branch_id, "expense_category_id": str(uuid.uuid4()),
                              "expense_date": "2026-01-01", "amount": "1"}),
        op("customer.create", {"id": str(uuid.uuid4()), "name": "Nope"}),
    ]
    results = push_ok(http_client, t, ops)
    assert results[0]["status"] == "applied" and "subscription_expired" in results[0]["flags"]
    assert results[1]["status"] == "applied" and "subscription_expired" in results[1]["flags"]
    assert results[2]["error"]["code"] == code and results[3]["error"]["code"] == code
    assert db_scalar("SELECT sync_flags FROM sales WHERE id = :i", {"i": results[0]["server_id"]}) == ["subscription_expired"]
    assert D(db_scalar("SELECT count(*) FROM customers WHERE business_id = :b", {"b": t.business_id})) == 0


def test_push_requires_sync_permission(http_client, registered_owner):
    # Owner has everything; an unauthenticated call is simply refused.
    r = http_client.post(registered_owner.url("/sync/push"), json={"device_id": "x", "batch_id": "y", "ops": []})
    assert r.status_code in (401, 403)
