"""Branch-assigned staff only reach their own branch(es); owners and business-wide staff see all.

``user_roles.branch_id`` is honoured: a role scoped to a branch grants that branch only.
Every denied call is paired with the same call on the user's own branch succeeding, so a 403
here is the branch rule and not a missing permission.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from tests.helpers.db import db_execute, db_scalar
from tests.helpers.phase2 import add_user, open_shift_for, post_sale, stock_in
from tests.helpers.tenants import build_pos_tenant, create_supplier

pytestmark = pytest.mark.integration

FOREIGN = "You do not have access to this branch"


class World:
    """One tenant, branches A (owner's) and B, a manager confined to each, data in both."""

    pass


@pytest.fixture(scope="module")
def w(api_base, require_server):
    import httpx

    with httpx.Client(timeout=60.0) as c:
        suffix = uuid.uuid4().int % 10_000_000
        t = build_pos_tenant(c, api_base, suffix, phone_prefix="560")
        world = World()
        world.c, world.t, world.api = c, t, api_base
        create_supplier(c, t)
        world.A = t.branch_id
        r = c.post(t.url("/branches"), headers=t.owner_headers, json={"name": "Branch B"})
        assert r.status_code == 201, r.text
        world.B = r.json()["id"]
        r = c.post(t.url("/branches"), headers=t.owner_headers, json={"name": "Branch C"})
        world.C = r.json()["id"]

        world.mgr_a = add_user(c, t, "manager", branch_id=world.A)
        world.mgr_b = add_user(c, t, "manager", branch_id=world.B)
        world.owner = t.owner_headers
        world.wide = t.manager_headers  # business-wide manager (no branch)

        # branch B: stock, a register + the B manager's shift, one completed sale
        stock_in(c, t, t.product_id, "50", "20", branch_id=world.B)
        world.reg_b, world.shift_b = open_shift_for(c, t, world.mgr_b, branch_id=world.B)
        sale_b = post_sale(c, t, headers=world.mgr_b, branch_id=world.B)
        assert sale_b.status_code == 201, sale_b.text
        world.sale_b = sale_b.json()["id"]
        sale_a = post_sale(c, t)  # owner's shift is on branch A
        assert sale_a.status_code == 201, sale_a.text
        world.sale_a = sale_a.json()["id"]
        world.reg_a = t.register_id
        world.shift_a = t.shift_id

        # records the A manager must not see: expense, PO, transfer B->C, adjustment in B
        cat = c.post(t.url("/expenses/categories"), headers=t.owner_headers, json={"name": "Rent", "code": "RNT"})
        world.cat = cat.json()["id"]
        exp_b = c.post(t.url("/expenses"), headers=t.owner_headers, json={
            "branch_id": world.B, "expense_category_id": world.cat,
            "expense_date": date.today().isoformat(), "amount": "10"})
        assert exp_b.status_code == 201, exp_b.text
        world.expense_b = exp_b.json()["id"]
        exp_a = c.post(t.url("/expenses"), headers=t.owner_headers, json={
            "branch_id": world.A, "expense_category_id": world.cat,
            "expense_date": date.today().isoformat(), "amount": "10"})
        world.expense_a = exp_a.json()["id"]
        po = c.post(t.url("/purchases/orders"), headers=t.owner_headers, json={
            "branch_id": world.B, "supplier_id": t.supplier_id,
            "lines": [{"product_id": t.product_id, "ordered_qty": "5", "cost_per_unit": "10"}]})
        assert po.status_code == 201, po.text
        world.po_b = po.json()["id"]
        tr = c.post(t.url("/transfers"), headers=t.owner_headers, json={
            "source_branch_id": world.B, "dest_branch_id": world.C,
            "lines": [{"product_id": t.product_id, "qty": "2"}]})
        assert tr.status_code == 201, tr.text
        world.transfer_bc = tr.json()["id"]
        tr2 = c.post(t.url("/transfers"), headers=t.owner_headers, json={
            "source_branch_id": world.A, "dest_branch_id": world.B,
            "lines": [{"product_id": t.product_id, "qty": "1"}]})
        assert tr2.status_code == 201, tr2.text
        world.transfer_ab = tr2.json()["id"]
        yield world


def get(w, headers, path, **params):
    return w.c.get(w.t.url(path), headers=headers, params=params)


def post(w, headers, path, body=None):
    return w.c.post(w.t.url(path), headers=headers, json=body or {})


def assert_foreign(resp):
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == FOREIGN


# ── sales ─────────────────────────────────────────────────────────────────────


def test_sales_list_is_pinned_to_the_users_branch(w):
    mine = get(w, w.mgr_a, "/sales").json()
    ids = {s["id"] for s in mine["items"]}
    assert w.sale_a in ids and w.sale_b not in ids
    assert {s["branch_id"] for s in mine["items"]} == {w.A}
    assert get(w, w.mgr_a, "/sales", branch_id=w.A).status_code == 200
    assert_foreign(get(w, w.mgr_a, "/sales", branch_id=w.B))


def test_owner_and_business_wide_staff_see_every_branch(w):
    for headers in (w.owner, w.wide):
        ids = {s["id"] for s in get(w, headers, "/sales").json()["items"]}
        assert {w.sale_a, w.sale_b} <= ids
        assert get(w, headers, "/sales", branch_id=w.B).status_code == 200


def test_sale_by_id_and_mutations_respect_branch(w):
    assert get(w, w.mgr_a, f"/sales/{w.sale_a}").status_code == 200
    assert_foreign(get(w, w.mgr_a, f"/sales/{w.sale_b}"))
    assert_foreign(post(w, w.mgr_a, f"/sales/{w.sale_b}/void"))
    assert get(w, w.owner, f"/sales/{w.sale_b}").status_code == 200
    assert get(w, w.mgr_b, f"/sales/{w.sale_b}").status_code == 200


def test_creating_a_sale_or_preview_in_a_foreign_branch_is_refused(w):
    body = {"branch_id": w.B, "lines": [{"product_id": w.t.product_id, "qty": "1", "unit_price": "100"}],
            "payments": [{"payment_method": "cash", "amount": "100"}]}
    assert_foreign(post(w, w.mgr_a, "/sales", body))
    assert_foreign(post(w, w.mgr_a, "/sales/price-preview", body))
    assert_foreign(post(w, w.mgr_a, "/sales/open-tab", {"branch_id": w.B}))
    own = post(w, w.mgr_a, "/sales/price-preview", {**body, "branch_id": w.A})
    assert own.status_code == 200, own.text


def test_returns_follow_branch(w):
    assert_foreign(get(w, w.mgr_a, "/returns", branch_id=w.B))
    assert get(w, w.mgr_a, "/returns").status_code == 200
    ret = {"branch_id": w.B, "sale_id": w.sale_b,
           "lines": [{"sale_line_id": str(uuid.uuid4()), "qty": "1"}]}
    assert_foreign(post(w, w.mgr_a, "/returns", ret))


# ── shifts and registers ──────────────────────────────────────────────────────


def test_shifts(w):
    assert_foreign(get(w, w.mgr_a, "/shifts", branch_id=w.B))
    listed = get(w, w.mgr_a, "/shifts").json()
    assert listed and {s["branch_id"] for s in listed} == {w.A}
    assert get(w, w.owner, "/shifts", branch_id=w.B).status_code == 200
    assert_foreign(get(w, w.mgr_a, f"/shifts/{w.shift_b}"))
    assert_foreign(get(w, w.mgr_a, f"/shifts/{w.shift_b}/summary"))
    assert_foreign(post(w, w.mgr_a, f"/shifts/{w.shift_b}/close", {"actual_cash": "0"}))
    assert_foreign(post(w, w.mgr_a, f"/shifts/{w.shift_b}/cash-movement", {"tx_type": "cash_in", "amount": "1"}))
    assert get(w, w.owner, f"/shifts/{w.shift_b}").status_code == 200


def test_cannot_open_a_shift_on_a_foreign_branch_register(w):
    assert_foreign(post(w, w.mgr_a, "/shifts/open", {"cash_register_id": w.reg_b, "opening_float": "0"}))


def test_registers(w):
    assert_foreign(get(w, w.mgr_a, "/registers", branch_id=w.B))
    regs = get(w, w.mgr_a, "/registers").json()
    assert w.reg_a in {r["id"] for r in regs} and w.reg_b not in {r["id"] for r in regs}
    assert_foreign(get(w, w.mgr_a, f"/registers/{w.reg_b}"))
    assert_foreign(get(w, w.mgr_a, f"/registers/{w.reg_b}/active-shift"))
    assert get(w, w.mgr_a, f"/registers/{w.reg_a}").status_code == 200
    assert_foreign(post(w, w.mgr_a, "/registers", {"name": "Sneaky", "branch_id": w.B}))
    # on its own branch the branch rule passes and the missing registers.manage permission is what refuses
    own = post(w, w.mgr_a, "/registers", {"name": f"Mine {uuid.uuid4().hex[:4]}", "branch_id": w.A})
    assert own.status_code == 403 and "Permission required" in own.json()["detail"]


# ── stock, adjustments, waste ─────────────────────────────────────────────────


def test_stock_reads(w):
    pid = w.t.product_id
    assert get(w, w.mgr_a, "/stock/balance", branch_id=w.A, product_id=pid).status_code == 200
    assert_foreign(get(w, w.mgr_a, "/stock/balance", branch_id=w.B, product_id=pid))
    assert_foreign(get(w, w.mgr_a, "/stock/balances", branch_id=w.B))
    assert get(w, w.mgr_a, "/stock/balances", branch_id=w.A).status_code == 200
    assert_foreign(get(w, w.mgr_a, "/stock/movements", branch_id=w.B))
    mv = get(w, w.mgr_a, "/stock/movements").json()
    assert {m["branch_id"] for m in (mv if isinstance(mv, list) else mv.get("items", []))} <= {w.A}
    assert get(w, w.owner, "/stock/balance", branch_id=w.B, product_id=pid).status_code == 200


def test_adjustments_and_waste(w):
    pid = w.t.product_id
    adj = {"reason": "count_correction", "lines": [{"product_id": pid, "qty_delta": "-1"}]}
    assert_foreign(post(w, w.mgr_a, "/adjustments", {**adj, "branch_id": w.B}))
    assert post(w, w.mgr_a, "/adjustments", {**adj, "branch_id": w.A}).status_code == 201
    assert_foreign(get(w, w.mgr_a, "/adjustments", branch_id=w.B))
    waste = {"reason": "expiry", "lines": [{"product_id": pid, "qty": "1"}]}
    assert_foreign(post(w, w.mgr_a, "/waste", {**waste, "branch_id": w.B}))
    assert post(w, w.mgr_a, "/waste", {**waste, "branch_id": w.A}).status_code == 201
    assert_foreign(get(w, w.mgr_a, "/waste", branch_id=w.B))


# ── transfers ─────────────────────────────────────────────────────────────────


def test_transfers(w):
    pid = w.t.product_id
    line = [{"product_id": pid, "qty": "1"}]
    assert_foreign(post(w, w.mgr_a, "/transfers", {"source_branch_id": w.B, "dest_branch_id": w.A, "lines": line}))
    assert post(w, w.mgr_a, "/transfers", {"source_branch_id": w.A, "dest_branch_id": w.B, "lines": line}).status_code == 201
    assert_foreign(get(w, w.mgr_a, "/transfers", branch_id=w.C))
    # visible at both ends, invisible to a third branch
    assert get(w, w.mgr_a, f"/transfers/{w.transfer_ab}").status_code == 200
    assert get(w, w.mgr_b, f"/transfers/{w.transfer_ab}").status_code == 200
    assert_foreign(get(w, w.mgr_a, f"/transfers/{w.transfer_bc}"))
    # only the destination can receive
    assert w.c.post(w.t.url(f"/transfers/{w.transfer_ab}/receive"), headers=w.mgr_a).status_code == 403
    assert_foreign(w.c.post(w.t.url(f"/transfers/{w.transfer_bc}/receive"), headers=w.mgr_a))
    assert w.c.post(w.t.url(f"/transfers/{w.transfer_ab}/receive"), headers=w.mgr_b).status_code == 200


# ── expenses and purchases ────────────────────────────────────────────────────


def test_expenses(w):
    body = {"expense_category_id": w.cat, "expense_date": date.today().isoformat(), "amount": "5"}
    assert_foreign(post(w, w.mgr_a, "/expenses", {**body, "branch_id": w.B}))
    assert post(w, w.mgr_a, "/expenses", {**body, "branch_id": w.A}).status_code == 201
    assert_foreign(get(w, w.mgr_a, "/expenses", branch_id=w.B))
    ids = {e["id"] for e in get(w, w.mgr_a, "/expenses").json()["items"]}
    assert w.expense_a in ids and w.expense_b not in ids
    assert get(w, w.mgr_a, f"/expenses/{w.expense_a}").status_code == 200
    assert_foreign(get(w, w.mgr_a, f"/expenses/{w.expense_b}"))
    assert_foreign(w.c.put(w.t.url(f"/expenses/{w.expense_b}"), headers=w.mgr_a, json={"amount": "6"}))
    assert_foreign(w.c.delete(w.t.url(f"/expenses/{w.expense_b}"), headers=w.mgr_a))
    assert_foreign(post(w, w.mgr_a, f"/expenses/{w.expense_b}/payments",
                        {"payment_method": "cash", "amount": "1"}))
    assert get(w, w.owner, f"/expenses/{w.expense_b}").status_code == 200


def test_purchases(w):
    line = [{"product_id": w.t.product_id, "ordered_qty": "1", "cost_per_unit": "1"}]
    base = {"supplier_id": w.t.supplier_id, "lines": line}
    assert_foreign(post(w, w.mgr_a, "/purchases/orders", {**base, "branch_id": w.B}))
    assert post(w, w.mgr_a, "/purchases/orders", {**base, "branch_id": w.A}).status_code == 201
    assert_foreign(get(w, w.mgr_a, "/purchases/orders", branch_id=w.B))
    assert_foreign(get(w, w.mgr_a, f"/purchases/orders/{w.po_b}"))
    assert get(w, w.owner, f"/purchases/orders/{w.po_b}").status_code == 200
    grn = {"branch_id": w.B, "supplier_id": w.t.supplier_id,
           "lines": [{"product_id": w.t.product_id, "qty_received": "1", "cost_per_unit": "1"}]}
    assert_foreign(post(w, w.mgr_a, "/purchases/receipts", grn))


def test_reports_follow_branch(w):
    assert_foreign(get(w, w.mgr_a, "/analytics/dashboard", branch_id=w.B))
    assert get(w, w.mgr_a, "/analytics/dashboard", branch_id=w.A).status_code == 200
    assert get(w, w.mgr_a, "/analytics/dashboard").status_code == 200  # pinned to A
    assert get(w, w.owner, "/analytics/dashboard", branch_id=w.B).status_code == 200


# ── branch list, roles scoped through user_roles.branch_id ────────────────────


def test_branch_listing_and_access(w):
    mine = get(w, w.mgr_a, "/branches").json()
    assert [b["id"] for b in mine] == [w.A]
    assert {b["id"] for b in get(w, w.owner, "/branches").json()} >= {w.A, w.B, w.C}
    assert get(w, w.mgr_a, f"/branches/{w.A}").status_code == 200
    assert_foreign(get(w, w.mgr_a, f"/branches/{w.B}"))
    assert_foreign(w.c.put(w.t.url(f"/branches/{w.B}"), headers=w.mgr_a, json={"name": "Hijack"}))


def test_user_roles_branch_id_is_honoured_without_a_default_branch(w):
    """Scoped role only (default branch cleared): access is exactly the role's branch."""
    scoped = add_user(w.c, w.t, "manager", branch_id=w.B)
    me = w.c.get(w.t.url("/auth/me"), headers=scoped).json()
    db_execute("UPDATE users SET default_branch_id = NULL WHERE id = :i", {"i": me["id"]})
    assert db_scalar("SELECT COUNT(*) FROM user_roles WHERE user_id = :i AND branch_id = :b",
                     {"i": me["id"], "b": w.B}) == 1
    assert get(w, scoped, "/sales", branch_id=w.B).status_code == 200
    assert_foreign(get(w, scoped, "/sales", branch_id=w.A))
    assert {s["branch_id"] for s in get(w, scoped, "/sales").json()["items"]} == {w.B}


def test_user_with_two_scoped_branches_must_choose(w):
    two = add_user(w.c, w.t, "manager", branch_id=w.A)
    me = w.c.get(w.t.url("/auth/me"), headers=two).json()
    db_execute(
        """
        INSERT INTO user_roles (id, business_id, user_id, role_id, branch_id, created_at, updated_at)
        SELECT gen_random_uuid(), business_id, user_id, role_id, :b, NOW(), NOW()
          FROM user_roles WHERE user_id = :i LIMIT 1
        """,
        {"b": w.B, "i": me["id"]},
    )
    r = get(w, two, "/sales")
    assert r.status_code == 400 and "branch_id is required" in r.json()["detail"]
    assert get(w, two, "/sales", branch_id=w.A).status_code == 200
    assert get(w, two, "/sales", branch_id=w.B).status_code == 200
    assert_foreign(get(w, two, "/sales", branch_id=w.C))


def test_assigning_a_branch_from_another_business_is_rejected(w, api_base):
    other = build_pos_tenant(w.c, api_base, uuid.uuid4().int % 10_000_000, phone_prefix="561")
    roles = w.c.get(w.t.url("/roles"), headers=w.owner).json()
    role_id = next(r["id"] for r in roles if r["name"].lower() == "cashier")
    r = w.c.post(w.t.url("/users"), headers=w.owner, json={
        "full_name": "Bad", "phone": f"6{uuid.uuid4().int % 10**9:09d}", "password": w.t.password,
        "role_ids": [role_id], "branch_id": other.branch_id})
    assert r.status_code == 400


def test_moving_a_user_to_another_branch_moves_their_scope(w):
    u = add_user(w.c, w.t, "manager", branch_id=w.A)
    me = w.c.get(w.t.url("/auth/me"), headers=u).json()
    assert_foreign(get(w, u, "/sales", branch_id=w.B))
    r = w.c.put(w.t.url(f"/users/{me['id']}"), headers=w.owner, json={"branch_id": w.B})
    assert r.status_code == 200, r.text
    assert get(w, u, "/sales", branch_id=w.B).status_code == 200
    assert_foreign(get(w, u, "/sales", branch_id=w.A))


def test_permissions_still_apply_inside_the_branch(w):
    """Being on the right branch does not grant permissions the role lacks."""
    cashier_a = add_user(w.c, w.t, "cashier", branch_id=w.A)
    assert get(w, cashier_a, "/sales", branch_id=w.A).status_code == 200
    assert post(w, cashier_a, f"/sales/{w.sale_a}/void").status_code == 403  # no sales.void
    assert_foreign(get(w, cashier_a, "/sales", branch_id=w.B))
