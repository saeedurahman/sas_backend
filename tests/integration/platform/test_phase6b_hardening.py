"""Integration tests for Phase 6b: Platform hardening and branch scoping.

Covers:
1. Platform admin TOTP secrets encrypted at rest in DB.
2. Platform admin change-password flow and session invalidation.
3. Server-side token revocation on logout (jti blacklist).
4. One-time recovery codes: generation, login, and single-use consumption.
5. Settings branch isolation (branch overrides, global restriction, foreign 403).
6. Customer ledger and payment branch scoping & shift branch check.
7. Supplier ledger and payment branch scoping.
8. Restaurant tables, floor plans, and open tab branch/shift validation.
9. Notifications branch scoping and foreign delete rejection.
10. Tenant export audit log recording.
11. Sync push branch scoping for customer payments.
"""

from __future__ import annotations

import time
import uuid
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.core.platform_security import is_fernet_token
from tests.helpers.assertions import assert_status
from tests.helpers.db import db_execute, db_rows, db_scalar
from tests.helpers.phase2 import add_user, create_customer, open_shift_for, post_sale
from tests.helpers.platform import (
    AdminCtx,
    audit_rows,
    code_for,
    create_admin_row,
    login_response,
    new_admin,
    root_url,
    sign_in,
)
from tests.helpers.sync import op, push
from tests.helpers.tenants import build_pos_tenant, create_supplier

pytestmark = pytest.mark.integration

FOREIGN = "You do not have access to this branch"


# ══════════════════════════════════════════════════════════════════════════════
# 1. Platform Hardening
# ══════════════════════════════════════════════════════════════════════════════


def test_totp_secret_encrypted_at_rest(http_client: httpx.Client, api_base: str):
    """TOTP secrets stored in platform_admins must be Fernet-encrypted strings."""
    root = root_url(api_base)
    admin_ctx = create_admin_row(root)
    stored_secret = db_scalar(
        "SELECT totp_secret FROM platform_admins WHERE id = :id",
        {"id": admin_ctx.id},
    )
    assert stored_secret is not None
    # Must NOT be stored in plain text (length > 16 base32 and is a valid Fernet token)
    assert stored_secret != admin_ctx.totp_secret
    assert is_fernet_token(stored_secret) is True

    # Login should still succeed by decrypting on the fly
    resp = login_response(http_client, admin_ctx)
    assert resp.status_code == 200, resp.text


def test_change_password_flow_and_session_invalidation(
    http_client: httpx.Client, api_base: str
):
    """Admin can change password, invalidating older tokens (token_version bumped)."""
    root = root_url(api_base)
    admin_ctx = new_admin(http_client, root)
    old_headers = dict(admin_ctx.headers)

    # 1. Wrong current password -> 400
    r = http_client.post(
        admin_ctx.url("/auth/change-password"),
        headers=old_headers,
        json={"current_password": "WrongPassword123!", "new_password": "NewValidPassword1!"},
    )
    assert r.status_code == 400, r.text

    # 2. Too short new password (< 12 chars) -> 422
    r = http_client.post(
        admin_ctx.url("/auth/change-password"),
        headers=old_headers,
        json={"current_password": admin_ctx.password, "new_password": "Short1!"},
    )
    assert r.status_code == 422, r.text

    # 3. Same as current password -> 400
    r = http_client.post(
        admin_ctx.url("/auth/change-password"),
        headers=old_headers,
        json={"current_password": admin_ctx.password, "new_password": admin_ctx.password},
    )
    assert r.status_code == 400, r.text

    # 4. Valid password change
    new_pw = "BrandNewSecurePassword123!"
    r = http_client.post(
        admin_ctx.url("/auth/change-password"),
        headers=old_headers,
        json={"current_password": admin_ctx.password, "new_password": new_pw},
    )
    assert r.status_code == 200, r.text
    new_token = r.json()["access_token"]
    assert new_token

    # 5. Old token should now be rejected (token_version bumped)
    r_old = http_client.get(admin_ctx.url("/me"), headers=old_headers)
    assert r_old.status_code == 401, r_old.text
    assert "no longer valid" in r_old.json()["detail"].lower()

    # 6. New token should work
    r_new = http_client.get(
        admin_ctx.url("/me"),
        headers={"Authorization": f"Bearer {new_token}"},
    )
    assert r_new.status_code == 200, r_new.text

    # 7. Audit log exists
    audits = audit_rows("auth.password_change", entity_id=admin_ctx.id)
    assert len(audits) >= 1


def test_server_side_logout_revokes_token(http_client: httpx.Client, api_base: str):
    """Calling POST /platform/auth/logout blacklists token jti; subsequent calls 401."""
    root = root_url(api_base)
    admin_ctx = new_admin(http_client, root)
    token_headers = dict(admin_ctx.headers)

    # Calling /me works before logout
    r = http_client.get(admin_ctx.url("/me"), headers=token_headers)
    assert r.status_code == 200, r.text

    # Call logout
    r_logout = http_client.post(admin_ctx.url("/auth/logout"), headers=token_headers)
    assert r_logout.status_code == 200, r_logout.text

    # Calling /me with the same token now returns 401 (revoked)
    r_after = http_client.get(admin_ctx.url("/me"), headers=token_headers)
    assert r_after.status_code == 401, r_after.text
    assert "revoked" in r_after.json()["detail"].lower()

    # Check revoked tokens table has an entry
    revoked_count = db_scalar(
        "SELECT COUNT(*) FROM platform_revoked_tokens WHERE admin_id = :id",
        {"id": admin_ctx.id},
    )
    assert revoked_count >= 1

    # Audit log has auth.logout
    audits = audit_rows("auth.logout", entity_id=admin_ctx.id)
    assert len(audits) >= 1


def test_recovery_codes_generation_and_single_use(
    http_client: httpx.Client, api_base: str
):
    """Admin can generate 10 recovery codes, use one instead of TOTP, and each code is single-use."""
    root = root_url(api_base)
    admin_ctx = new_admin(http_client, root)

    # 1. Generate recovery codes
    r = http_client.post(admin_ctx.url("/auth/recovery-codes"), headers=admin_ctx.headers)
    assert r.status_code == 200, r.text
    codes = r.json()["recovery_codes"]
    assert len(codes) == 10
    code_to_use = codes[0]

    # Verify hashes stored in DB
    hashes_in_db = db_scalar(
        "SELECT recovery_codes_hashes FROM platform_admins WHERE id = :id",
        {"id": admin_ctx.id},
    )
    assert hashes_in_db is not None
    assert len(hashes_in_db) == 10

    # 2. Login using the recovery code instead of TOTP
    r_login = http_client.post(
        admin_ctx.url("/auth/login"),
        json={
            "email": admin_ctx.email,
            "password": admin_ctx.password,
            "recovery_code": code_to_use,
        },
    )
    assert r_login.status_code == 200, r_login.text
    login_data = r_login.json()
    assert "access_token" in login_data

    # Audit log has auth.recovery_code_used
    audits = audit_rows("auth.recovery_code_used", entity_id=admin_ctx.id)
    assert len(audits) >= 1

    # DB now has 9 recovery codes remaining
    hashes_after = db_scalar(
        "SELECT recovery_codes_hashes FROM platform_admins WHERE id = :id",
        {"id": admin_ctx.id},
    )
    assert len(hashes_after) == 9

    # 3. Trying to use the same recovery code again should fail (401)
    r_reuse = http_client.post(
        admin_ctx.url("/auth/login"),
        json={
            "email": admin_ctx.email,
            "password": admin_ctx.password,
            "recovery_code": code_to_use,
        },
    )
    assert r_reuse.status_code == 401, r_reuse.text
    assert "invalid" in r_reuse.json()["detail"].lower()


# ══════════════════════════════════════════════════════════════════════════════
# 2. Branch Scoping Gaps
# ══════════════════════════════════════════════════════════════════════════════


class MultiBranchTenant:
    pass


@pytest.fixture(scope="module")
def mb(api_base: str):
    """Fixture providing a tenant with branches A and B, plus scoped managers."""
    with httpx.Client(timeout=60.0) as c:
        suffix = uuid.uuid4().int % 10_000_000
        t = build_pos_tenant(c, api_base, suffix, phone_prefix="570")
        obj = MultiBranchTenant()
        obj.c, obj.t, obj.api = c, t, api_base
        obj.A = t.branch_id

        # Enable restaurant table management and dining features
        c.put(
            f"{api_base}/business/config",
            headers=t.owner_headers,
            json={"config_json": {"enable_table_management": True, "enable_restaurant": True}},
        )

        # Grant settings.manage and restaurant.* to Manager role in this tenant
        roles = c.get(t.url("/roles"), headers=t.owner_headers).json()
        mgr_role = next(r for r in roles if r["name"].lower() == "manager")
        current_keys = mgr_role.get("permission_keys", [])
        new_keys = list(
            set(current_keys)
            | {
                "settings.manage",
                "restaurant.floor_plans.view",
                "restaurant.floor_plans.manage",
                "restaurant.tables.view",
                "restaurant.tables.manage",
            }
        )
        c.put(
            t.url(f"/roles/{mgr_role['id']}/permissions"),
            headers=t.owner_headers,
            json={"permission_keys": new_keys},
        )

        # Create branch B
        rb = c.post(t.url("/branches"), headers=t.owner_headers, json={"name": "Branch B"})
        assert rb.status_code == 201, rb.text
        obj.B = rb.json()["id"]

        # Users
        obj.mgr_a = add_user(c, t, "manager", branch_id=obj.A)
        obj.mgr_b = add_user(c, t, "manager", branch_id=obj.B)
        obj.owner = t.owner_headers

        # Shifts
        obj.reg_a = t.register_id
        obj.shift_a = t.shift_id
        obj.reg_b, obj.shift_b = open_shift_for(c, t, obj.mgr_b, branch_id=obj.B)

        # Customer and Supplier
        obj.customer_id = create_customer(c, t)
        obj.supplier_id = create_supplier(c, t)
        yield obj


def test_settings_branch_scoping(mb: MultiBranchTenant):
    """Branch manager cannot mutate global settings or foreign branch settings."""
    # 1. Manager A cannot update business-wide settings (branch_id=None)
    r = mb.c.put(
        mb.t.url("/settings"),
        headers=mb.mgr_a,
        json={
            "setting_key": "printer_config",
            "setting_value": {"model": "EPSON"},
            "branch_id": None,
        },
    )
    assert r.status_code == 403, r.text
    assert "business-wide" in r.json()["detail"].lower()

    # 2. Manager A cannot update foreign branch B settings
    r = mb.c.put(
        mb.t.url("/settings"),
        headers=mb.mgr_a,
        json={
            "setting_key": "printer_config",
            "setting_value": {"model": "EPSON"},
            "branch_id": mb.B,
        },
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == FOREIGN

    # 3. Manager A CAN update own branch A settings
    r = mb.c.put(
        mb.t.url("/settings"),
        headers=mb.mgr_a,
        json={
            "setting_key": "printer_config",
            "setting_value": {"model": "EPSON"},
            "branch_id": mb.A,
        },
    )
    assert r.status_code == 200, r.text

    # 4. Manager A cannot read branch B settings
    r = mb.c.get(
        mb.t.url("/settings"),
        headers=mb.mgr_a,
        params={"branch_id": mb.B},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == FOREIGN

    # 5. Manager A can read own branch A settings
    r = mb.c.get(
        mb.t.url("/settings"),
        headers=mb.mgr_a,
        params={"branch_id": mb.A},
    )
    assert r.status_code == 200, r.text


def test_customer_ledger_and_payments_branch_scoping(mb: MultiBranchTenant):
    """Customer payment respects branch_id and shift.branch_id; ledger filters by branch."""
    # 1. Customer payment with foreign branch_id gives 403
    r = mb.c.post(
        mb.t.url(f"/customers/{mb.customer_id}/payments"),
        headers=mb.mgr_a,
        json={
            "amount": "100.00",
            "payment_method": "cash",
            "branch_id": mb.B,
            "register_shift_id": mb.shift_a,
        },
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == FOREIGN

    # 2. Manager A cannot use shift from branch B (no access to shift B's branch) -> 403
    r = mb.c.post(
        mb.t.url(f"/customers/{mb.customer_id}/payments"),
        headers=mb.mgr_a,
        json={
            "amount": "100.00",
            "payment_method": "cash",
            "branch_id": mb.A,
            "register_shift_id": mb.shift_b,
        },
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == FOREIGN

    # 3. Owner (who has access to all branches) attempting mismatch between branch and shift -> 400
    r_mismatch = mb.c.post(
        mb.t.url(f"/customers/{mb.customer_id}/payments"),
        headers=mb.owner,
        json={
            "amount": "100.00",
            "payment_method": "cash",
            "branch_id": mb.A,
            "register_shift_id": mb.shift_b,
        },
    )
    assert r_mismatch.status_code == 400, r_mismatch.text
    assert "shift" in r_mismatch.json()["detail"].lower()

    # 4. Customer payment with own branch and shift succeeds, stamps branch_id
    r = mb.c.post(
        mb.t.url(f"/customers/{mb.customer_id}/payments"),
        headers=mb.mgr_a,
        json={
            "amount": "150.00",
            "payment_method": "cash",
            "branch_id": mb.A,
            "register_shift_id": mb.shift_a,
        },
    )
    assert r.status_code == 201, r.text
    payment_data = r.json()
    assert payment_data["ledger_entry_id"] is not None

    # 5. Check DB row has branch_id set
    ledger_branch = db_scalar(
        "SELECT branch_id FROM customer_ledger WHERE customer_id = :c ORDER BY entry_at DESC LIMIT 1",
        {"c": mb.customer_id},
    )
    assert str(ledger_branch) == mb.A

    # 6. Ledger list with foreign branch gives 403
    r = mb.c.get(
        mb.t.url(f"/customers/{mb.customer_id}/ledger"),
        headers=mb.mgr_a,
        params={"branch_id": mb.B},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == FOREIGN

    # 7. Ledger list with own branch gives 200
    r = mb.c.get(
        mb.t.url(f"/customers/{mb.customer_id}/ledger"),
        headers=mb.mgr_a,
        params={"branch_id": mb.A},
    )
    assert r.status_code == 200, r.text


def test_supplier_ledger_and_payments_branch_scoping(mb: MultiBranchTenant):
    """Supplier payment respects branch_id; ledger filters by branch."""
    # 1. Supplier payment with foreign branch_id gives 403
    r = mb.c.post(
        mb.t.url(f"/supplier-ledger/{mb.supplier_id}/payment"),
        headers=mb.mgr_a,
        json={
            "amount": "200.00",
            "payment_method": "cash",
            "branch_id": mb.B,
        },
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == FOREIGN

    # 2. Supplier payment with own branch succeeds, stamps branch_id
    r = mb.c.post(
        mb.t.url(f"/supplier-ledger/{mb.supplier_id}/payment"),
        headers=mb.mgr_a,
        json={
            "amount": "250.00",
            "payment_method": "cash",
            "branch_id": mb.A,
        },
    )
    assert r.status_code == 201, r.text
    sp_data = r.json()
    assert sp_data["branch_id"] == mb.A

    # 3. Check DB row has branch_id set
    s_branch = db_scalar(
        "SELECT branch_id FROM supplier_ledger WHERE supplier_id = :s ORDER BY entry_at DESC LIMIT 1",
        {"s": mb.supplier_id},
    )
    assert str(s_branch) == mb.A

    # 4. Supplier ledger list with foreign branch gives 403
    r = mb.c.get(
        mb.t.url(f"/supplier-ledger/{mb.supplier_id}"),
        headers=mb.mgr_a,
        params={"branch_id": mb.B},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == FOREIGN

    # 5. Supplier ledger list with own branch gives 200
    r = mb.c.get(
        mb.t.url(f"/supplier-ledger/{mb.supplier_id}"),
        headers=mb.mgr_a,
        params={"branch_id": mb.A},
    )
    assert r.status_code == 200, r.text


def test_restaurant_tables_and_tabs_branch_scoping(mb: MultiBranchTenant):
    """Restaurant floor plans, tables, and open tab respect branch and shift."""
    # 1. Create a floor plan on branch B as owner
    rfp = mb.c.post(
        mb.t.url("/restaurant/floor-plans"),
        headers=mb.owner,
        json={"name": "Patio B", "branch_id": mb.B},
    )
    assert rfp.status_code == 201, rfp.text
    fp_b_id = rfp.json()["id"]

    # Create table on floor plan B
    rtbl = mb.c.post(
        mb.t.url("/restaurant/tables"),
        headers=mb.owner,
        json={
            "name": "Table B1",
            "table_number": "B1",
            "capacity": 4,
            "floor_plan_id": fp_b_id,
            "branch_id": mb.B,
        },
    )
    assert rtbl.status_code == 201, rtbl.text
    tbl_b_id = rtbl.json()["id"]

    # Create floor plan & table on branch A for open-tab testing
    rfp_a = mb.c.post(
        mb.t.url("/restaurant/floor-plans"),
        headers=mb.owner,
        json={"name": "Patio A", "branch_id": mb.A},
    )
    assert rfp_a.status_code == 201, rfp_a.text
    fp_a_id = rfp_a.json()["id"]
    rtbl_a = mb.c.post(
        mb.t.url("/restaurant/tables"),
        headers=mb.owner,
        json={
            "name": "Table A1",
            "table_number": "A1",
            "capacity": 4,
            "floor_plan_id": fp_a_id,
            "branch_id": mb.A,
        },
    )
    assert rtbl_a.status_code == 201, rtbl_a.text
    tbl_a_id = rtbl_a.json()["id"]

    # 2. Manager A cannot view floor plans for branch B
    r = mb.c.get(
        mb.t.url("/restaurant/floor-plans"),
        headers=mb.mgr_a,
        params={"branch_id": mb.B},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == FOREIGN

    # 3. Manager A listing tables automatically scopes or filters out B
    r = mb.c.get(mb.t.url("/restaurant/tables"), headers=mb.mgr_a)
    assert r.status_code == 200, r.text
    tables = r.json()
    assert tbl_b_id not in [t["id"] for t in tables]

    # 4. Manager A querying table B detail directly gets 403
    r = mb.c.get(mb.t.url(f"/restaurant/tables/{tbl_b_id}"), headers=mb.mgr_a)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == FOREIGN

    # 5. Open tab with foreign branch_id gives 403
    r = mb.c.post(
        mb.t.url("/sales/open-tab"),
        headers=mb.mgr_a,
        json={"branch_id": mb.B, "table_id": tbl_b_id, "register_shift_id": mb.shift_a},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == FOREIGN

    # 6. Open tab with own branch but foreign shift gives 400
    r = mb.c.post(
        mb.t.url("/sales/open-tab"),
        headers=mb.owner,
        json={"branch_id": mb.A, "table_id": tbl_a_id, "register_shift_id": mb.shift_b},
    )
    assert r.status_code == 400, r.text
    assert "shift" in r.json()["detail"].lower()


def test_notifications_branch_scoping(mb: MultiBranchTenant):
    """User only sees notifications for their branch (or global null branch); foreign delete 403."""
    notif_b_id = str(uuid.uuid4())
    db_execute(
        """
        INSERT INTO notification_log (id, business_id, branch_id, notification_type, channel, title, body)
        VALUES (:id, :b, :br, 'system', 'in_app', 'Branch B Alert', 'Only for branch B')
        """,
        {"id": notif_b_id, "b": mb.t.business_id, "br": mb.B},
    )

    notif_a_id = str(uuid.uuid4())
    db_execute(
        """
        INSERT INTO notification_log (id, business_id, branch_id, notification_type, channel, title, body)
        VALUES (:id, :b, :br, 'system', 'in_app', 'Branch A Alert', 'For branch A')
        """,
        {"id": notif_a_id, "b": mb.t.business_id, "br": mb.A},
    )

    # 1. Manager A lists notifications -> sees A, does NOT see B
    r = mb.c.get(mb.t.url("/notifications"), headers=mb.mgr_a)
    assert r.status_code == 200, r.text
    notif_ids = [n["id"] for n in r.json()["items"]]
    assert notif_a_id in notif_ids
    assert notif_b_id not in notif_ids

    # 2. Manager A attempts to delete notification from branch B -> 403
    r = mb.c.delete(mb.t.url(f"/notifications/{notif_b_id}"), headers=mb.mgr_a)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == FOREIGN

    # 3. Manager A deletes notification from branch A -> 200
    r = mb.c.delete(mb.t.url(f"/notifications/{notif_a_id}"), headers=mb.mgr_a)
    assert r.status_code == 200, r.text


# ══════════════════════════════════════════════════════════════════════════════
# 3. Tenant Export Audit Entry
# ══════════════════════════════════════════════════════════════════════════════


def test_export_audit_entry(mb: MultiBranchTenant):
    """Exporting a dataset generates an audit log row with action='export' and metadata."""
    # Run an export
    r = mb.c.get(
        mb.t.url("/export/sales"),
        headers=mb.owner,
        params={"format": "csv", "date_from": "2025-01-01", "date_to": "2026-12-31"},
    )
    assert r.status_code == 200, r.text

    # Verify audit log row
    row = db_rows(
        """
        SELECT * FROM audit_logs
         WHERE business_id = :b AND action = 'export' AND table_name = 'sales'
         ORDER BY created_at DESC LIMIT 1
        """,
        {"b": mb.t.business_id},
    )
    assert len(row) == 1
    audit = row[0]
    assert audit["table_name"] == "sales"
    assert audit["action"] == "export"
    new_vals = audit["new_values"]
    assert new_vals["dataset"] == "sales"
    assert new_vals["format"] == "csv"
    assert new_vals["date_from"] == "2025-01-01"
    assert new_vals["date_to"] == "2026-12-31"
    assert "row_count" in new_vals


# ══════════════════════════════════════════════════════════════════════════════
# 4. Sync Push Branch Scoping
# ══════════════════════════════════════════════════════════════════════════════


def test_sync_push_customer_payment_branch_scoping(mb: MultiBranchTenant):
    """Sync push verifies branch scope for customer payments."""
    pay_op_foreign = op(
        "customer.payment",
        {
            "id": str(uuid.uuid4()),
            "customer_id": mb.customer_id,
            "branch_id": mb.B,
            "amount": "50.00",
            "payment_method": "cash",
            "register_shift_id": mb.shift_a,
        },
    )
    # Manager A pushes payment for branch B -> rejected with branch_forbidden
    resp = push(mb.c, mb.t, [pay_op_foreign], headers=mb.mgr_a)
    assert resp.status_code == 200, resp.text
    res = resp.json()["results"][0]
    assert res["status"] == "rejected"
    assert res["error"]["code"] == "branch_forbidden"

    pay_op_own = op(
        "customer.payment",
        {
            "id": str(uuid.uuid4()),
            "customer_id": mb.customer_id,
            "branch_id": mb.A,
            "amount": "50.00",
            "payment_method": "cash",
            "register_shift_id": mb.shift_a,
        },
    )
    # Manager A pushes payment for branch A -> applied and stamps branch_id
    resp = push(mb.c, mb.t, [pay_op_own], headers=mb.mgr_a)
    assert resp.status_code == 200, resp.text
    res = resp.json()["results"][0]
    assert res["status"] == "applied"

    # Verify stamped branch_id in customer_ledger
    stamped_branch = db_scalar(
        "SELECT branch_id FROM customer_ledger WHERE id = :i",
        {"i": pay_op_own["payload"]["id"]},
    )
    assert str(stamped_branch) == mb.A
