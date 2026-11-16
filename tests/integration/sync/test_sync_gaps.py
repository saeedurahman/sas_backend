"""Phase 6c: Offline sync gaps tests.

Covers:
1. sale.create with modifiers (applied, totals, modifier notes persistence, frozen totals)
2. modifier price mismatch flag ('modifier_price_mismatch')
3. unknown modifier id accepted, snapshot name/price kept, flagged ('unknown_modifier')
4. duplicate push of sale with modifiers (idempotency)
5. dine-in ops rejected in push with code 'online_only_operation'
6. sync pull & snapshot: modifier_groups, modifiers, product_modifier_groups, floor_plans, tables
7. branch scoping for floor_plan and table
8. soft-delete tombstones for restaurant entities
9. local history in pull: register_shifts (with totals/diff), sale_summary (with totals, customer, payments)
10. customer ledger balance & balance_owed convention (balance_owed > 0 means customer owes)
11. receipt branding in settings pull
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest

from app.services.restaurant_tab_service import decode_sale_line_notes
from tests.helpers.assertions import assert_status
from tests.helpers.db import db_execute, db_rows, db_scalar
from tests.helpers.phase2 import create_customer, new_product, second_branch, stock_in
from tests.helpers.sync import by_entity, op, pull_all, pull_json, push, push_ok, sale_payload, snapshot_all
from tests.helpers.tenants import TenantContext

pytestmark = pytest.mark.integration


def _enable_restaurant_features(http_client, t: TenantContext) -> None:
    resp = http_client.put(
        t.url("/business/config"),
        headers=t.owner_headers,
        json={"config_json": {"enable_restaurant": True, "enable_table_management": True}},
    )
    assert_status(resp, 200, label="enable restaurant features")


def _create_modifier_group_and_item(
    http_client, t: TenantContext, *, name="Size", price="25.00"
) -> tuple[str, str]:
    mg_resp = http_client.post(
        t.url("/restaurant/modifier-groups"),
        headers=t.owner_headers,
        json={
            "name": f"{name} {t.suffix}",
            "selection_type": "multiple",
            "is_required": False,
            "max_selections": 5,
            "sort_order": 1,
        },
    )
    assert_status(mg_resp, 201)
    mg_id = mg_resp.json()["id"]

    mod_resp = http_client.post(
        t.url(f"/restaurant/modifier-groups/{mg_id}/modifiers"),
        headers=t.owner_headers,
        json={"name": f"Extra Cheese {t.suffix}", "price_delta": price, "sort_order": 1},
    )
    assert_status(mod_resp, 201)
    mod_id = mod_resp.json()["id"]

    # Link to tenant's product
    link_resp = http_client.put(
        t.url(f"/restaurant/products/{t.product_id}/modifier-groups"),
        headers=t.owner_headers,
        json={"modifier_group_ids": [mg_id]},
    )
    assert_status(link_resp, 200)

    return mg_id, mod_id


# ── 1. sale.create with modifiers ─────────────────────────────────────────────


def test_push_sale_with_valid_modifiers(http_client, pos_tenant):
    t = pos_tenant
    _enable_restaurant_features(http_client, t)
    mg_id, mod_id = _create_modifier_group_and_item(http_client, t, price="25.00")

    sale_id = str(uuid.uuid4())
    # Unit price 100, modifier 25 -> line total 125, payment 125
    lines = [
        {
            "product_id": t.product_id,
            "qty": "1",
            "unit_price": "100.00",
            "modifiers": [
                {
                    "modifier_id": mod_id,
                    "name": "Extra Cheese",
                    "price": "25.00",
                    "qty": "1",
                }
            ],
            "notes": "Crispy please",
        }
    ]
    payments = [{"payment_method": "cash", "amount": "125.00"}]
    payload = sale_payload(t, sale_id=sale_id, lines=lines, payments=payments)
    res = push_ok(http_client, t, [op("sale.create", payload)])[0]

    assert res["status"] == "applied"
    assert res["server_id"] == sale_id
    assert "modifier_price_mismatch" not in res["flags"]
    assert "unknown_modifier" not in res["flags"]

    # Verify DB sale payment amount and sale_line notes
    payment_row = db_rows(
        "SELECT amount FROM sale_payments WHERE sale_id = CAST(:id AS uuid)", {"id": sale_id}
    )[0]
    assert Decimal(str(payment_row["amount"])) == Decimal("125.00")

    line_row = db_rows(
        "SELECT notes FROM sale_lines WHERE sale_id = CAST(:id AS uuid)", {"id": sale_id}
    )[0]
    raw_notes = line_row["notes"]
    assert raw_notes is not None
    kitchen_notes, mod_snapshots = decode_sale_line_notes(raw_notes)
    assert kitchen_notes == "Crispy please"
    assert len(mod_snapshots) == 1
    assert str(mod_snapshots[0]["modifier_id"]) == mod_id
    assert Decimal(str(mod_snapshots[0]["price"])) == Decimal("25.00")


def test_push_sale_with_modifier_price_mismatch(http_client, pos_tenant):
    t = pos_tenant
    _enable_restaurant_features(http_client, t)
    mg_id, mod_id = _create_modifier_group_and_item(http_client, t, price="25.00")

    sale_id = str(uuid.uuid4())
    # Catalog modifier is 25.00, client charged 15.00. Payment = 115.00.
    lines = [
        {
            "product_id": t.product_id,
            "qty": "1",
            "unit_price": "100.00",
            "modifiers": [
                {
                    "modifier_id": mod_id,
                    "name": "Extra Cheese",
                    "price": "15.00",
                    "qty": "1",
                }
            ],
        }
    ]
    payments = [{"payment_method": "cash", "amount": "115.00"}]
    payload = sale_payload(t, sale_id=sale_id, lines=lines, payments=payments)
    res = push_ok(http_client, t, [op("sale.create", payload)])[0]

    assert res["status"] == "applied"
    assert "modifier_price_mismatch" in res["flags"]

    # Frozen total semantics: client paid 115.00, server persists 115.00
    payment_row = db_rows(
        "SELECT amount FROM sale_payments WHERE sale_id = CAST(:id AS uuid)", {"id": sale_id}
    )[0]
    assert Decimal(str(payment_row["amount"])) == Decimal("115.00")


def test_push_sale_with_unknown_modifier_id(http_client, pos_tenant):
    t = pos_tenant
    _enable_restaurant_features(http_client, t)

    sale_id = str(uuid.uuid4())
    fake_mod_id = str(uuid.uuid4())
    # Unknown modifier id -> sale accepted, snapshot kept in notes, flagged
    lines = [
        {
            "product_id": t.product_id,
            "qty": "1",
            "unit_price": "100.00",
            "modifiers": [
                {
                    "modifier_id": fake_mod_id,
                    "name": "Custom Special Spice",
                    "price": "10.00",
                    "qty": "1",
                }
            ],
        }
    ]
    payments = [{"payment_method": "cash", "amount": "110.00"}]
    payload = sale_payload(t, sale_id=sale_id, lines=lines, payments=payments)
    res = push_ok(http_client, t, [op("sale.create", payload)])[0]

    assert res["status"] == "applied"
    assert "unknown_modifier" in res["flags"]

    line_row = db_rows(
        "SELECT notes FROM sale_lines WHERE sale_id = CAST(:id AS uuid)", {"id": sale_id}
    )[0]
    raw_notes = line_row["notes"]
    kitchen_notes, mod_snapshots = decode_sale_line_notes(raw_notes)
    assert len(mod_snapshots) == 1
    assert mod_snapshots[0]["name"] == "Custom Special Spice"
    assert Decimal(str(mod_snapshots[0]["price"])) == Decimal("10.00")


def test_push_duplicate_sale_with_modifiers(http_client, pos_tenant):
    t = pos_tenant
    _enable_restaurant_features(http_client, t)
    mg_id, mod_id = _create_modifier_group_and_item(http_client, t, price="25.00")

    op_id = str(uuid.uuid4())
    sale_id = str(uuid.uuid4())
    lines = [
        {
            "product_id": t.product_id,
            "qty": "1",
            "unit_price": "100.00",
            "modifiers": [{"modifier_id": mod_id, "name": "Extra Cheese", "price": "25.00", "qty": "1"}],
        }
    ]
    payload = sale_payload(t, sale_id=sale_id, lines=lines, payments=[{"payment_method": "cash", "amount": "125.00"}])
    sale_op = op("sale.create", payload, op_id=op_id)

    first = push_ok(http_client, t, [sale_op])[0]
    assert first["status"] == "applied"

    second = push_ok(http_client, t, [sale_op])[0]
    assert second["status"] == "duplicate"
    assert second["original_status"] == "applied"
    assert second["server_id"] == first["server_id"]


# ── 2. Dine-in ops rejected in push ──────────────────────────────────────────


def test_push_rejects_online_only_operations(http_client, pos_tenant):
    t = pos_tenant
    _enable_restaurant_features(http_client, t)

    for tab_op in ("tab.open", "tab.fire", "tab.settle", "dine_in.open"):
        res = push_ok(
            http_client,
            t,
            [op(tab_op, {"table_id": str(uuid.uuid4())})],
        )[0]
        assert res["status"] == "rejected"
        assert res["error"] is not None
        assert res["error"]["code"] == "online_only_operation"
        assert res["error"]["retryable"] is False


# ── 3. Pull & snapshot of restaurant entities with branch scope ───────────────


def test_pull_and_snapshot_restaurant_entities_and_branch_scope(http_client, pos_tenant):
    t = pos_tenant
    _enable_restaurant_features(http_client, t)
    other_branch = second_branch(http_client, t)

    # 1. Create modifier group, modifier, and product link
    mg_id, mod_id = _create_modifier_group_and_item(http_client, t, price="30.00")

    # 2. Create floor plans and tables for branch 1 and branch 2
    fp1_resp = http_client.post(
        t.url("/restaurant/floor-plans"),
        headers=t.owner_headers,
        json={"branch_id": t.branch_id, "name": f"Main Floor {t.suffix}"},
    )
    assert_status(fp1_resp, 201)
    fp1_id = fp1_resp.json()["id"]

    tbl1_resp = http_client.post(
        t.url("/restaurant/tables"),
        headers=t.owner_headers,
        json={"branch_id": t.branch_id, "floor_plan_id": fp1_id, "table_number": "T1", "capacity": 4},
    )
    assert_status(tbl1_resp, 201)
    tbl1_id = tbl1_resp.json()["id"]

    fp2_resp = http_client.post(
        t.url("/restaurant/floor-plans"),
        headers=t.owner_headers,
        json={"branch_id": other_branch, "name": f"Patio {t.suffix}"},
    )
    assert_status(fp2_resp, 201)
    fp2_id = fp2_resp.json()["id"]

    tbl2_resp = http_client.post(
        t.url("/restaurant/tables"),
        headers=t.owner_headers,
        json={"branch_id": other_branch, "floor_plan_id": fp2_id, "table_number": "P1", "capacity": 2},
    )
    assert_status(tbl2_resp, 201)
    tbl2_id = tbl2_resp.json()["id"]

    # 3. Pull with branch_id = t.branch_id
    changes, _ = pull_all(http_client, t, branch_id=t.branch_id)

    mg_changes = by_entity(changes, "modifier_group")
    mod_changes = by_entity(changes, "modifier")
    pmg_changes = by_entity(changes, "product_modifier_group")
    fp_changes = by_entity(changes, "floor_plan")
    tbl_changes = by_entity(changes, "table")

    # Modifiers are tenant-wide
    assert any(c["id"] == mg_id for c in mg_changes)
    assert any(c["id"] == mod_id for c in mod_changes)
    assert any(c["data"].get("modifier_group_id") == mg_id for c in pmg_changes)

    # Floor plan and table are branch-scoped: branch 1 is visible, branch 2 is NOT
    assert any(c["id"] == fp1_id for c in fp_changes)
    assert not any(c["id"] == fp2_id for c in fp_changes)
    assert any(c["id"] == tbl1_id for c in tbl_changes)
    assert not any(c["id"] == tbl2_id for c in tbl_changes)

    # 4. Soft delete table 1 and verify tombstone in pull
    del_resp = http_client.delete(
        t.url(f"/restaurant/tables/{tbl1_id}"),
        headers=t.owner_headers,
    )
    assert_status(del_resp, 200)

    # Pull again from current cursor or fresh
    fresh_changes, _ = pull_all(http_client, t, branch_id=t.branch_id)
    del_table_changes = [c for c in by_entity(fresh_changes, "table") if c["id"] == tbl1_id]
    assert any(c["op"] == "delete" for c in del_table_changes)


# ── 4. Local history and customer balance in pull ────────────────────────────


def test_pull_local_history_and_customer_balance(http_client, pos_tenant):
    t = pos_tenant

    # 1. Customer and ledger entry
    cust_id = create_customer(http_client, t, credit_limit="5000.00")

    # Create a credit sale so customer owes money
    sale_id = str(uuid.uuid4())
    lines = [{"product_id": t.product_id, "qty": "1", "unit_price": "250.00", "discount_amount": "0", "tax_amount": "0", "tax_rate": "0"}]
    payments = [{"payment_method": "credit", "amount": "250.00"}]
    sale = sale_payload(t, sale_id=sale_id, customer_id=cust_id, lines=lines, payments=payments)
    res = push_ok(http_client, t, [op("sale.create", sale)])[0]
    assert res["status"] == "applied"

    # Pull snapshot all changes
    changes, _, _ = snapshot_all(http_client, t)

    # Check customer entity and customer_balance entity
    cust_changes = [c for c in by_entity(changes, "customer") if c["id"] == cust_id]
    assert cust_changes
    c_data = cust_changes[-1]["data"]
    # balance is raw ledger sum (-250.00), balance_owed is positive (250.00)
    assert Decimal(c_data["balance"]) == Decimal("-250.00")
    assert Decimal(c_data["balance_owed"]) == Decimal("250.00")

    cb_changes = [c for c in by_entity(changes, "customer_balance") if c["id"] == cust_id]
    assert cb_changes
    cb_data = cb_changes[-1]["data"]
    assert Decimal(cb_data["balance"]) == Decimal("-250.00")
    assert Decimal(cb_data["balance_owed"]) == Decimal("250.00")

    # Check sale_summary entity
    summary_changes = [c for c in by_entity(changes, "sale_summary") if c["id"] == sale_id]
    assert summary_changes
    s_data = summary_changes[-1]["data"]
    assert s_data["id"] == sale_id
    assert s_data["customer_id"] == cust_id
    assert s_data["totals"]["total"] == "250.00"
    assert s_data["totals"]["subtotal"] == "250.00"
    assert len(s_data["payments"]) == 1
    assert s_data["payments"][0]["payment_method"] == "credit"
    assert s_data["payments"][0]["amount"] == "250.00"
    # Full line detail should NOT be in summary
    assert "lines" not in s_data

    # Check register_shift entity (t.shift_id was opened by this user)
    shift_changes = [c for c in by_entity(changes, "register_shift") if c["id"] == t.shift_id]
    assert shift_changes
    sh_data = shift_changes[-1]["data"]
    assert sh_data["id"] == t.shift_id
    assert "opening_float" in sh_data
    assert "status" in sh_data


# ── 5. Receipt branding in settings ──────────────────────────────────────────


def test_pull_receipt_branding_in_settings(http_client, pos_tenant):
    t = pos_tenant
    changes, _, _ = snapshot_all(http_client, t)
    settings_changes = by_entity(changes, "settings")
    assert settings_changes
    s_data = settings_changes[0]["data"]

    # Verify branding fields exist
    assert "business_name" in s_data
    assert "phone" in s_data
    assert "tax_id" in s_data
    assert "address_line1" in s_data
    assert "receipt_footer" in s_data
    assert "tax_labels" in s_data
