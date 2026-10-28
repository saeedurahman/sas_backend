"""Tenant audit trail (actor, branch, before/after) and the permissions_version fingerprint."""

from __future__ import annotations

import json
import re
import uuid

import pytest

from tests.helpers.db import db_rows
from tests.helpers.phase2 import add_user, post_sale
from tests.helpers.tenants import build_pos_tenant, login

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def t(api_base, require_server):
    import httpx

    with httpx.Client(timeout=60.0) as c:
        tenant = build_pos_tenant(c, api_base, uuid.uuid4().int % 10_000_000, phone_prefix="570")
        tenant.client = c  # type: ignore[attr-defined]
        yield tenant


def audit(t, table: str, record_id: str | None = None, event: str | None = None):
    sql = "SELECT * FROM audit_logs WHERE business_id = :b AND table_name = :t"
    params = {"b": t.business_id, "t": table}
    if record_id:
        sql += " AND record_id = :r"
        params["r"] = record_id
    rows = db_rows(sql + " ORDER BY created_at", params)
    if event:
        rows = [r for r in rows if (r["new_values"] or {}).get("event") == event or (r["new_values"] or {}).get("action") == event]
    return rows


def me(t, headers):
    return t.client.get(t.url("/auth/me"), headers=headers).json()


def test_void_is_audited_with_actor_branch_and_before_after(t):
    sale = post_sale(t.client, t).json()
    owner_id = me(t, t.owner_headers)["id"]
    r = t.client.post(t.url(f"/sales/{sale['id']}/void"), headers=t.owner_headers)
    assert r.status_code == 200, r.text
    row = audit(t, "sales", sale["id"], "void")[0]
    assert str(row["user_id"]) == owner_id and str(row["branch_id"]) == t.branch_id
    assert row["action"] == "update"
    assert row["old_values"]["status"] == "completed" and row["old_values"]["total"] == "100.00"
    assert row["new_values"]["status"] == "voided" and row["new_values"]["voided_by"] == owner_id


def test_return_is_audited(t):
    sale = post_sale(t.client, t).json()
    r = t.client.post(t.url("/returns"), headers=t.owner_headers, json={
        "branch_id": t.branch_id, "sale_id": sale["id"],
        "lines": [{"sale_line_id": sale["lines"][0]["id"], "qty": "1"}],
        "refund_payments": [{"payment_method": "cash", "amount": "100"}]})
    assert r.status_code == 201, r.text
    row = audit(t, "sale_returns", r.json()["id"])[0]
    assert row["action"] == "create" and str(row["branch_id"]) == t.branch_id
    assert row["old_values"]["sale_id"] == sale["id"]
    assert float(row["new_values"]["total_refund"]) == 100 and float(row["new_values"]["cash_out"]) == 100
    assert row["new_values"]["lines"][0]["qty"] == "1"


def test_cancel_is_audited(t):
    sale = post_sale(t.client, t, qty="1", unit_price="10").json()
    # completed sales cannot be cancelled (void instead); a draft tab can: fall back to the audit of the refusal path
    r = t.client.put(t.url(f"/sales/{sale['id']}/cancel"), headers=t.owner_headers)
    assert r.status_code == 400
    from tests.helpers.db import db_execute

    db_execute("UPDATE sales SET status = 'draft' WHERE id = :i", {"i": sale["id"]})
    r = t.client.put(t.url(f"/sales/{sale['id']}/cancel"), headers=t.owner_headers)
    assert r.status_code == 200, r.text
    row = audit(t, "sales", sale["id"], "cancel")[0]
    assert row["old_values"]["status"] == "draft" and row["new_values"]["status"] == "cancelled"
    assert str(row["branch_id"]) == t.branch_id


def test_price_change_is_audited_with_old_and_new_price(t):
    lst = t.client.post(t.url("/prices/lists"), headers=t.owner_headers,
                        json={"name": f"Retail {uuid.uuid4().hex[:4]}", "list_type": "retail"})
    assert lst.status_code == 201, lst.text
    url = t.url(f"/prices/lists/{lst.json()['id']}/items")
    first = t.client.post(url, headers=t.owner_headers, json={"product_id": t.product_id, "unit_price": "120"})
    assert first.status_code in (200, 201), first.text
    second = t.client.post(url, headers=t.owner_headers, json={"product_id": t.product_id, "unit_price": "135"})
    assert second.status_code in (200, 201)
    item_id = second.json()["id"]
    rows = audit(t, "price_list_items", item_id)
    assert [r["action"] for r in rows] == ["create", "update"]
    assert rows[0]["old_values"] is None and rows[0]["new_values"]["unit_price"] == "120"
    assert rows[1]["old_values"] == {"unit_price": "120.00"} and rows[1]["new_values"]["unit_price"] == "135"
    assert all(r["user_id"] is not None for r in rows)


def test_role_and_permission_changes_are_audited(t):
    roles = t.client.get(t.url("/roles"), headers=t.owner_headers).json()
    cashier = next(r for r in roles if r["name"].lower() == "cashier")
    keep = [k for k in cashier["permission_keys"] if k != "sales.view"]
    put = t.client.put(t.url(f"/roles/{cashier['id']}/permissions"), headers=t.owner_headers,
                       json={"permission_keys": keep})
    assert put.status_code == 200, put.text
    row = audit(t, "role_permissions", cashier["id"])[-1]
    assert row["action"] == "update" and row["user_id"] is not None
    assert "sales.view" in row["old_values"]["permission_keys"]
    assert "sales.view" not in row["new_values"]["permission_keys"]
    assert row["new_values"]["removed"] == ["sales.view"] and row["new_values"]["added"] == []
    # put it back so later tests see the standard cashier
    t.client.put(t.url(f"/roles/{cashier['id']}/permissions"), headers=t.owner_headers,
                 json={"permission_keys": cashier["permission_keys"]})

    created = t.client.post(t.url("/roles"), headers=t.owner_headers,
                            json={"name": f"Auditor {uuid.uuid4().hex[:4]}", "permission_keys": ["sales.view"]})
    assert created.status_code == 201, created.text
    rid = created.json()["id"]
    renamed = t.client.put(t.url(f"/roles/{rid}"), headers=t.owner_headers, json={"description": "reads sales"})
    assert renamed.status_code == 200
    assert t.client.delete(t.url(f"/roles/{rid}"), headers=t.owner_headers).status_code in (200, 204)
    assert [r["action"] for r in audit(t, "roles", rid)] == ["create", "update", "delete"]
    upd = audit(t, "roles", rid)[1]
    assert upd["old_values"]["description"] != upd["new_values"]["description"]


def test_user_role_assignment_is_audited(t):
    u = add_user(t.client, t, "cashier", branch_id=t.branch_id)
    uid = me(t, u)["id"]
    roles = t.client.get(t.url("/roles"), headers=t.owner_headers).json()
    manager = next(r for r in roles if r["name"].lower() == "manager")
    r = t.client.put(t.url(f"/users/{uid}/roles"), headers=t.owner_headers, json={"role_ids": [manager["id"]]})
    assert r.status_code == 200, r.text
    row = audit(t, "user_roles", uid)[-1]
    assert [r.lower() for r in row["old_values"]["roles"]] == ["cashier"]
    assert [r.lower() for r in row["new_values"]["roles"]] == ["manager"]
    assert str(row["branch_id"]) == t.branch_id


def test_user_deactivation_reset_and_deletion_are_audited(t):
    u = add_user(t.client, t, "cashier", branch_id=t.branch_id)
    uid = me(t, u)["id"]
    owner_id = me(t, t.owner_headers)["id"]

    r = t.client.put(t.url(f"/users/{uid}/password"), headers=t.owner_headers, json={"new_password": "ResetPass9"})
    assert r.status_code == 200, r.text
    reset = audit(t, "users", uid, "password_reset")[0]
    assert str(reset["user_id"]) == owner_id and str(reset["branch_id"]) == t.branch_id

    r = t.client.put(t.url(f"/users/{uid}"), headers=t.owner_headers, json={"is_active": False})
    assert r.status_code == 200, r.text
    off = audit(t, "users", uid, "user_deactivated")[0]
    assert str(off["user_id"]) == owner_id and str(off["branch_id"]) == t.branch_id
    assert off["old_values"]["status"] == "active" and off["new_values"]["status"] == "inactive"

    r = t.client.put(t.url(f"/users/{uid}"), headers=t.owner_headers, json={"is_active": True})
    assert audit(t, "users", uid, "user_reactivated")

    assert t.client.delete(t.url(f"/users/{uid}"), headers=t.owner_headers).status_code in (200, 204)
    gone = audit(t, "users", uid, "user_deleted")[0]
    assert gone["action"] == "delete" and gone["old_values"]["status"] == "active"


def test_pin_reset_is_audited_with_branch(t):
    u = add_user(t.client, t, "cashier", branch_id=t.branch_id)
    uid = me(t, u)["id"]
    r = t.client.put(t.url(f"/users/{uid}/pin"), headers=t.owner_headers, json={"pin_code": "593827"})
    assert r.status_code == 200
    row = audit(t, "users", uid, "pin_reset")[0]
    assert str(row["branch_id"]) == t.branch_id and row["user_id"] is not None


def test_shift_close_with_a_difference_is_audited(t):
    from tests.helpers.phase2 import open_shift_for

    boss = add_user(t.client, t, "manager", branch_id=t.branch_id)
    _reg, shift_id = open_shift_for(t.client, t, boss)
    boss_id = me(t, boss)["id"]
    r = t.client.post(t.url(f"/shifts/{shift_id}/close"), headers=boss, json={"actual_cash": "37.50"})
    assert r.status_code == 200, r.text
    rows = audit(t, "register_shifts", shift_id)
    assert [x["new_values"]["event"] for x in rows] == ["shift_opened", "shift_closed"]
    closed = rows[1]
    assert str(closed["user_id"]) == boss_id and str(closed["branch_id"]) == t.branch_id
    assert closed["old_values"]["status"] == "open" and closed["old_values"]["expected_cash"] == "0.00"
    assert closed["new_values"]["actual"] == "37.50" and closed["new_values"]["difference"] == "37.50"
    assert closed["new_values"]["closed_other_users_shift"] is False


def test_closing_someone_elses_shift_is_flagged(t):
    from tests.helpers.phase2 import open_shift_for

    cashier = add_user(t.client, t, "cashier", branch_id=t.branch_id)
    _reg, shift_id = open_shift_for(t.client, t, cashier)
    r = t.client.post(t.url(f"/shifts/{shift_id}/close"), headers=t.owner_headers, json={"actual_cash": "0"})
    assert r.status_code == 200, r.text
    closed = audit(t, "register_shifts", shift_id, "shift_closed")[0]
    assert closed["new_values"]["closed_other_users_shift"] is True
    assert closed["new_values"]["closed_by"] == me(t, t.owner_headers)["id"]


def test_stock_adjustment_is_audited_with_quantities(t):
    owner_id = me(t, t.owner_headers)["id"]
    r = t.client.post(t.url("/adjustments"), headers=t.owner_headers, json={
        "branch_id": t.branch_id, "reason": "damage",
        "lines": [{"product_id": t.product_id, "qty_delta": "-3"}]})
    assert r.status_code == 201, r.text
    row = audit(t, "stock_adjustments", r.json()["id"])[0]
    assert str(row["user_id"]) == owner_id and str(row["branch_id"]) == t.branch_id
    line = row["new_values"]["lines"][0]
    assert float(line["qty_delta"]) == -3
    assert float(line["qty_before"]) - 3 == float(line["qty_after"])
    assert row["new_values"]["reason"] == "damage"
    assert row["old_values"]["qty_before"][t.product_id] == line["qty_before"]


def test_audit_api_exposes_branch_and_before_after(t):
    r = t.client.get(t.url("/audit"), headers=t.owner_headers, params={"table_name": "stock_adjustments"})
    assert r.status_code == 200, r.text
    body = r.json()
    items = body["items"] if isinstance(body, dict) else body
    assert items and items[0]["branch_id"] == t.branch_id
    assert items[0]["new_values"]["event"] == "stock_adjustment" and items[0]["user_name"]


# ── permissions_version ───────────────────────────────────────────────────────


def test_permissions_version_is_in_me_and_login_and_is_stable(t):
    a, b = me(t, t.cashier_headers), me(t, t.cashier_headers)
    assert re.fullmatch(r"[0-9a-f]{16}", a["permissions_version"])
    assert a["permissions_version"] == b["permissions_version"]
    login_body = t.client.post(t.url("/auth/login"), json={"phone": t.cashier_phone, "password": t.password}).json()
    assert login_body["user"]["permissions_version"] == a["permissions_version"]
    refreshed = login(t.client, t._api_base, t.cashier_phone, t.password)
    assert me(t, refreshed)["permissions_version"] == a["permissions_version"]


def test_permissions_version_differs_between_roles_and_tracks_role_edits(t):
    cashier, manager, owner = (me(t, h)["permissions_version"] for h in (t.cashier_headers, t.manager_headers, t.owner_headers))
    assert len({cashier, manager, owner}) == 3

    roles = t.client.get(t.url("/roles"), headers=t.owner_headers).json()
    role = next(r for r in roles if r["name"].lower() == "cashier")
    before = me(t, t.cashier_headers)["permissions_version"]
    t.client.put(t.url(f"/roles/{role['id']}/permissions"), headers=t.owner_headers,
                 json={"permission_keys": [k for k in role["permission_keys"] if k != "customers.view"]})
    during = me(t, t.cashier_headers)["permissions_version"]
    assert during != before
    t.client.put(t.url(f"/roles/{role['id']}/permissions"), headers=t.owner_headers,
                 json={"permission_keys": role["permission_keys"]})
    assert me(t, t.cashier_headers)["permissions_version"] == before  # same access, same version


def test_permissions_version_changes_when_branch_scope_changes(t):
    u = add_user(t.client, t, "manager")
    base = me(t, u)
    second = t.client.post(t.url("/branches"), headers=t.owner_headers, json={"name": "Scope Branch"}).json()["id"]
    t.client.put(t.url(f"/users/{base['id']}"), headers=t.owner_headers, json={"branch_id": second})
    assert me(t, u)["permissions_version"] != base["permissions_version"]


def test_me_has_the_documented_fields(t):
    body = me(t, t.owner_headers)
    assert {"id", "full_name", "phone", "business_id", "branch_id", "business_name", "business_type_code",
            "business_slug", "roles", "permission_keys", "permissions_version", "subscription"} <= set(body)
    assert set(body["subscription"]) == {"status", "days_left"}
    json.dumps(body)
