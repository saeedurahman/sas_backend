"""/platform/* business, subscription, payment, plan, settings, admin and audit endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services.subscription import add_months
from tests.helpers.db import db_execute, db_rows, db_scalar
from tests.helpers.platform import (
    audit_rows, business_row, code_for, create_admin_row, login_response, new_admin, set_subscription,
)
from tests.helpers.tenants import register_owner

pytestmark = pytest.mark.integration

DAY = timedelta(days=1)


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.fixture
def make_tenant(http_client, api_base):
    made = []

    def _make(prefix: str = "540", name: str | None = None):
        n = uuid.uuid4().int % 10_000_000
        t = register_owner(http_client, api_base, n, business_name=name or f"PA {n}",
                           owner_name="Owner Name", phone_prefix=prefix)
        made.append(t)
        return t

    return _make


@pytest.fixture
def settings_restored():
    keys = ("contact_phone", "contact_whatsapp", "contact_email", "payment_instructions")
    saved = {k: db_scalar("SELECT value FROM platform_settings WHERE key = :k", {"k": k}) or "" for k in keys}
    yield
    for k, v in saved.items():
        db_execute("UPDATE platform_settings SET value = :v WHERE key = :k", {"k": k, "v": v})


def _get(client, adm, path, **params):
    return client.get(adm.url(path), headers=adm.headers, params=params)


def _send(client, admin, method, path, body=None):
    return client.request(method, admin.url(path), headers=admin.headers, json=body)


# ── dashboard ─────────────────────────────────────────────────────────────────


def test_dashboard_counts_follow_business_states(http_client, admin, make_tenant):
    before = _get(http_client, admin, "/dashboard").json()
    assert set(before) == {
        "active", "trial", "expiring_7d", "grace", "suspended", "expired", "cancelled",
        "payments_this_month_pkr", "needs_attention",
    }
    trial_ok, active_ok, expiring, grace, suspended, expired = (make_tenant() for _ in range(6))
    set_subscription(trial_ok.business_id, status="trial", trial_ends_in=14 * DAY)
    set_subscription(active_ok.business_id, status="active", sub_ends_in=100 * DAY)
    set_subscription(expiring.business_id, status="active", sub_ends_in=3 * DAY)
    set_subscription(grace.business_id, status="active", sub_ends_in=-DAY)
    set_subscription(suspended.business_id, status="suspended", sub_ends_in=50 * DAY)
    set_subscription(expired.business_id, status="trial", trial_ends_in=-DAY)

    after = _get(http_client, admin, "/dashboard").json()
    delta = {k: after[k] - before[k] for k in ("active", "trial", "expiring_7d", "grace", "suspended", "expired")}
    # new tenants registered as trial; the SQL moved each into its bucket
    assert delta == {"active": 2, "trial": 1, "expiring_7d": 1, "grace": 1, "suspended": 1, "expired": 1}

    names = {i["business_id"]: i for i in after["needs_attention"]}
    assert expiring.business_id in names and grace.business_id in names
    assert active_ok.business_id not in names and trial_ok.business_id not in names
    item = names[grace.business_id]
    assert set(item) == {"business_id", "name", "owner_phone", "status", "expires_at", "days_left"}
    assert item["status"] == "grace" and item["owner_phone"] == grace.owner_phone
    assert item["days_left"] < 0


def test_dashboard_expiring_counts_trials_ending_within_seven_days(http_client, admin, make_tenant):
    before = _get(http_client, admin, "/dashboard").json()["expiring_7d"]
    t = make_tenant()
    set_subscription(t.business_id, status="trial", trial_ends_in=2 * DAY)
    assert _get(http_client, admin, "/dashboard").json()["expiring_7d"] == before + 1


# ── businesses list / detail ──────────────────────────────────────────────────


def test_businesses_list_shape_search_filter_sort_and_paging(http_client, admin, make_tenant):
    tag = uuid.uuid4().hex[:8]
    a, b, c = (make_tenant(name=f"Zeta-{tag}-{n}") for n in "abc")
    set_subscription(b.business_id, status="suspended")

    r = _get(http_client, admin, "/businesses", search=f"zeta-{tag}")
    assert r.status_code == 200
    page = r.json()
    assert set(page) == {"items", "total", "page", "page_size"} and page["total"] == 3
    row = next(i for i in page["items"] if i["id"] == a.business_id)
    assert set(row) == {
        "id", "name", "owner_phone", "plan_id", "plan_name", "status", "expires_at", "days_left",
        "branches", "users", "last_active_at", "created_at",
    }
    assert row["owner_phone"] == a.owner_phone and row["status"] == "trial"
    assert row["branches"] == 1 and row["users"] == 1  # just the registered owner
    assert 13 <= row["days_left"] <= 14

    only_suspended = _get(http_client, admin, "/businesses", search=tag, status="suspended").json()
    assert [i["id"] for i in only_suspended["items"]] == [b.business_id]

    by_phone = _get(http_client, admin, "/businesses", search=c.owner_phone).json()
    assert [i["id"] for i in by_phone["items"]] == [c.business_id]

    asc = [i["name"] for i in _get(http_client, admin, "/businesses", search=tag, sort="name").json()["items"]]
    desc = [i["name"] for i in _get(http_client, admin, "/businesses", search=tag, sort="name", order="desc").json()["items"]]
    assert asc == sorted(asc) and desc == list(reversed(asc))

    p1 = _get(http_client, admin, "/businesses", search=tag, sort="name", page_size=2, page=1).json()
    p2 = _get(http_client, admin, "/businesses", search=tag, sort="name", page_size=2, page=2).json()
    assert (len(p1["items"]), len(p2["items"]), p1["total"], p2["page"]) == (2, 1, 3, 2)
    assert {i["id"] for i in p1["items"]}.isdisjoint({i["id"] for i in p2["items"]})


def test_businesses_list_rejects_bad_sort_and_page_size(http_client, admin):
    assert _get(http_client, admin, "/businesses", sort="password_hash").status_code == 400
    assert _get(http_client, admin, "/businesses", page_size=1000).status_code == 422
    assert _get(http_client, admin, "/businesses", status="nonsense").status_code == 422


def test_search_treats_wildcards_literally(http_client, admin):
    assert _get(http_client, admin, "/businesses", search="%").json()["total"] == 0 or all(
        "%" in i["name"] or "%" in (i["owner_phone"] or "") for i in _get(http_client, admin, "/businesses", search="%").json()["items"]
    )


def test_counts_branches_users_and_last_activity(http_client, admin, make_tenant):
    t = make_tenant()
    assert http_client.post(t.url("/branches"), headers=t.owner_headers, json={"name": "Second"}).status_code == 201
    row = _get(http_client, admin, "/businesses", search=t.owner_phone).json()["items"][0]
    assert row["branches"] == 2 and row["users"] == 1
    assert row["last_active_at"] is not None  # registration issued a session


def test_business_detail_shape(http_client, admin, make_tenant):
    t = make_tenant()
    d = _get(http_client, admin, f"/businesses/{t.business_id}").json()
    assert set(d) == {
        "id", "name", "owner_name", "owner_phone", "owner_email", "address", "city", "is_active",
        "created_at", "branches", "users", "last_active_at", "subscription", "payments",
        "recent_audit", "notes",
    }
    assert d["owner_name"] == "Owner Name" and d["owner_phone"] == t.owner_phone
    assert set(d["subscription"]) == {
        "plan_id", "plan_name", "status", "stored_status", "trial_ends_at",
        "subscription_ends_at", "expires_at", "days_left", "in_grace",
    }
    assert d["subscription"]["status"] == "trial" and d["subscription"]["stored_status"] == "trial"
    assert d["payments"] == [] and d["notes"] == []
    assert _get(http_client, admin, f"/businesses/{uuid.uuid4()}").status_code == 404


def test_notes_are_stored_and_listed(http_client, admin, make_tenant):
    t = make_tenant()
    r = _send(http_client, admin, "POST", f"/businesses/{t.business_id}/notes", {"text": "Called, will pay Friday"})
    assert r.status_code == 201 and r.json()["admin_email"] == admin.email
    d = _get(http_client, admin, f"/businesses/{t.business_id}").json()
    assert [n["text"] for n in d["notes"]] == ["Called, will pay Friday"]
    assert _send(http_client, admin, "POST", f"/businesses/{t.business_id}/notes", {"text": ""}).status_code == 422


# ── payments ──────────────────────────────────────────────────────────────────

PAY = {"amount_pkr": "6000", "method": "jazzcash", "reference": "TX-1", "period_months": 2, "note": "first"}


def test_payment_extends_the_trial_by_calendar_months_and_activates(http_client, admin, make_tenant):
    t = make_tenant()
    trial_end = business_row(t.business_id)["trial_ends_at"]
    r = _send(http_client, admin, "POST", f"/businesses/{t.business_id}/payments", {**PAY, "reference": uuid.uuid4().hex})
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"payment", "subscription"}
    pay = body["payment"]
    assert set(pay) == {
        "id", "business_id", "plan_id", "amount_pkr", "method", "reference", "note", "period_months",
        "period_start", "period_end", "recorded_by", "recorded_by_email", "created_at",
    }
    assert pay["amount_pkr"] == 6000 and pay["method"] == "jazzcash" and pay["recorded_by_email"] == admin.email
    expected_end = add_months(trial_end, 2)  # starts when the trial ends: no days are lost
    assert abs((_iso(pay["period_end"]) - expected_end).total_seconds()) < 2
    sub = body["subscription"]
    assert sub["status"] == "active" and sub["stored_status"] == "active"
    assert abs((_iso(sub["subscription_ends_at"]) - expected_end).total_seconds()) < 2
    assert business_row(t.business_id)["subscription_status"] == "active"


def test_second_payment_continues_from_the_current_end(http_client, admin, make_tenant):
    t = make_tenant()
    set_subscription(t.business_id, status="active", sub_ends_in=20 * DAY)
    end1 = business_row(t.business_id)["subscription_ends_at"]
    r = _send(http_client, admin, "POST", f"/businesses/{t.business_id}/payments",
              {**PAY, "reference": uuid.uuid4().hex, "period_months": 1})
    assert abs((_iso(r.json()["subscription"]["subscription_ends_at"]) - add_months(end1, 1)).total_seconds()) < 2


def test_payment_reactivates_expired_and_suspended_businesses(http_client, admin, make_tenant):
    expired, suspended = make_tenant(), make_tenant()
    set_subscription(expired.business_id, status="active", sub_ends_in=-30 * DAY)
    set_subscription(suspended.business_id, status="suspended", sub_ends_in=-30 * DAY)
    assert http_client.get(expired.url("/products"), headers=expired.owner_headers).status_code == 402
    assert http_client.get(suspended.url("/products"), headers=suspended.owner_headers).status_code == 402
    for t in (expired, suspended):
        r = _send(http_client, admin, "POST", f"/businesses/{t.business_id}/payments",
                  {**PAY, "reference": uuid.uuid4().hex, "period_months": 1})
        assert r.status_code == 201
        sub = r.json()["subscription"]
        assert sub["status"] == "active"
        # lapsed time is not billed back: the new period runs one month from now
        assert 28 <= sub["days_left"] <= 31
        assert http_client.get(t.url("/products"), headers=t.owner_headers).status_code == 200


def test_payment_aliases_validation_and_duplicate_reference(http_client, admin, make_tenant):
    t = make_tenant()
    url = f"/businesses/{t.business_id}/payments"
    ok = _send(http_client, admin, "POST", url, {"amount": 1500, "method": "easypaisa", "reference": "EP-9", "months": 1})
    assert ok.status_code == 201 and ok.json()["payment"]["period_months"] == 1
    dup = _send(http_client, admin, "POST", url, {"amount": 1500, "method": "easypaisa", "reference": "EP-9", "months": 1})
    assert dup.status_code == 409
    assert db_scalar("SELECT COUNT(*) FROM subscription_payments WHERE business_id = :b", {"b": t.business_id}) == 1
    # same reference on another method is a different transaction
    assert _send(http_client, admin, "POST", url, {"amount": 10, "method": "bank", "reference": "EP-9", "months": 1}).status_code == 201
    # cash needs no reference and may repeat
    for _ in range(2):
        assert _send(http_client, admin, "POST", url, {"amount": 10, "method": "cash", "months": 1}).status_code == 201
    for bad in (
        {"amount_pkr": 0, "method": "cash", "period_months": 1},
        {"amount_pkr": -5, "method": "cash", "period_months": 1},
        {"amount_pkr": 5, "method": "cheque", "period_months": 1},
        {"amount_pkr": 5, "method": "cash", "period_months": 0},
        {"amount_pkr": 5, "method": "cash", "period_months": 37},
        {"amount_pkr": 5, "method": "cash"},
    ):
        assert _send(http_client, admin, "POST", url, bad).status_code == 422, bad
    assert _send(http_client, admin, "POST", f"/businesses/{uuid.uuid4()}/payments", PAY).status_code == 404


def test_payment_history_dashboard_total_and_audit(http_client, admin, make_tenant):
    t = make_tenant()
    before = _get(http_client, admin, "/dashboard").json()["payments_this_month_pkr"]
    ref = uuid.uuid4().hex
    r = _send(http_client, admin, "POST", f"/businesses/{t.business_id}/payments", {**PAY, "reference": ref})
    pid = r.json()["payment"]["id"]
    assert _get(http_client, admin, "/dashboard").json()["payments_this_month_pkr"] == before + 6000
    d = _get(http_client, admin, f"/businesses/{t.business_id}").json()
    assert [p["id"] for p in d["payments"]] == [pid]
    rows = audit_rows("payment.record", entity_id=pid)
    assert len(rows) == 1
    row = rows[0]
    assert row["admin_email"] == admin.email and str(row["admin_id"]) == admin.id
    assert str(row["business_id"]) == t.business_id
    assert row["before"]["status"] == "trial" and row["after"]["status"] == "active"
    assert row["after"]["reference"] == ref and row["after"]["period_months"] == 2
    assert row["before"]["subscription_ends_at"] is None and row["after"]["subscription_ends_at"]
    assert d["recent_audit"][0]["action"] == "payment.record"


def test_payment_with_plan_sets_the_businesses_plan(http_client, admin, make_tenant):
    t = make_tenant()
    plan = next(p for p in _get(http_client, admin, "/plans").json() if p["name"] == "Multi-branch")
    r = _send(http_client, admin, "POST", f"/businesses/{t.business_id}/payments",
              {**PAY, "reference": uuid.uuid4().hex, "plan_id": plan["id"]})
    assert r.json()["subscription"]["plan_name"] == "Multi-branch"
    assert _send(http_client, admin, "POST", f"/businesses/{t.business_id}/payments",
                 {**PAY, "reference": uuid.uuid4().hex, "plan_id": str(uuid.uuid4())}).status_code == 404


# ── subscription PATCH ────────────────────────────────────────────────────────


def test_patch_extend_days_moves_trial_or_paid_end(http_client, admin, make_tenant):
    t = make_tenant()
    trial_end = business_row(t.business_id)["trial_ends_at"]
    r = _send(http_client, admin, "PATCH", f"/businesses/{t.business_id}/subscription", {"extend_days": 10})
    assert r.status_code == 200 and r.json()["status"] == "trial"
    assert abs((_iso(r.json()["trial_ends_at"]) - (trial_end + 10 * DAY)).total_seconds()) < 2

    set_subscription(t.business_id, status="active", sub_ends_in=-5 * DAY)  # lapsed: extends from now
    r2 = _send(http_client, admin, "PATCH", f"/businesses/{t.business_id}/subscription", {"extend_days": 7})
    assert 6 <= r2.json()["days_left"] <= 7 and r2.json()["status"] == "active"


def test_patch_explicit_end_status_and_plan(http_client, admin, make_tenant):
    t = make_tenant()
    end = (datetime.now(timezone.utc) + 90 * DAY).replace(microsecond=0)
    plan = next(p for p in _get(http_client, admin, "/plans").json() if p["name"] == "Basic")
    r = _send(http_client, admin, "PATCH", f"/businesses/{t.business_id}/subscription",
              {"subscription_ends_at": end.isoformat(), "plan_id": plan["id"]})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "active" and body["plan_name"] == "Basic"
    assert _iso(body["subscription_ends_at"]) == end

    s = _send(http_client, admin, "PATCH", f"/businesses/{t.business_id}/subscription", {"status": "suspended"})
    assert s.json()["status"] == "suspended"
    assert http_client.get(t.url("/products"), headers=t.owner_headers).json()["code"] == "subscription_suspended"
    c = _send(http_client, admin, "PATCH", f"/businesses/{t.business_id}/subscription", {"status": "cancelled"})
    assert c.json()["status"] == "cancelled"
    assert http_client.get(t.url("/products"), headers=t.owner_headers).json()["code"] == "subscription_cancelled"


def test_patch_validation(http_client, admin, make_tenant):
    t = make_tenant()
    url = f"/businesses/{t.business_id}/subscription"
    end = (datetime.now(timezone.utc) + DAY).isoformat()
    assert _send(http_client, admin, "PATCH", url, {}).status_code == 422
    assert _send(http_client, admin, "PATCH", url, {"extend_days": 5, "subscription_ends_at": end}).status_code == 422
    assert _send(http_client, admin, "PATCH", url, {"extend_days": 0}).status_code == 422
    assert _send(http_client, admin, "PATCH", url, {"status": "grace"}).status_code == 422  # derived, not stored
    assert _send(http_client, admin, "PATCH", url, {"plan_id": str(uuid.uuid4())}).status_code == 404
    assert _send(http_client, admin, "PATCH", f"/businesses/{uuid.uuid4()}/subscription", {"extend_days": 1}).status_code == 404


def test_patch_audit_has_before_and_after(http_client, admin, make_tenant):
    t = make_tenant()
    _send(http_client, admin, "PATCH", f"/businesses/{t.business_id}/subscription", {"extend_days": 3, "note": "goodwill"})
    row = audit_rows("business.subscription_update", entity_id=t.business_id)[0]
    assert row["before"]["status"] == "trial" and row["after"]["note"] == "goodwill"
    assert row["before"]["trial_ends_at"] != row["after"]["trial_ends_at"]


def test_suspend_and_activate(http_client, admin, make_tenant):
    t = make_tenant()
    s = _send(http_client, admin, "POST", f"/businesses/{t.business_id}/suspend", {"reason": "chargeback"})
    assert s.status_code == 200 and s.json()["status"] == "suspended"
    assert http_client.get(t.url("/products"), headers=t.owner_headers).status_code == 402
    a = _send(http_client, admin, "POST", f"/businesses/{t.business_id}/activate")
    assert a.status_code == 200 and a.json()["status"] == "trial"  # back to its trial dates
    assert http_client.get(t.url("/products"), headers=t.owner_headers).status_code == 200
    assert _send(http_client, admin, "POST", f"/businesses/{t.business_id}/activate").status_code == 400
    sus = audit_rows("business.suspend", entity_id=t.business_id)[0]
    assert sus["after"]["reason"] == "chargeback" and sus["before"]["status"] == "trial"
    assert audit_rows("business.activate", entity_id=t.business_id)


def test_activate_restores_a_paid_business_to_active(http_client, admin, make_tenant):
    t = make_tenant()
    set_subscription(t.business_id, status="suspended", sub_ends_in=40 * DAY)
    assert _send(http_client, admin, "POST", f"/businesses/{t.business_id}/activate").json()["status"] == "active"


def test_suspending_does_not_delete_anything(http_client, admin, make_tenant):
    t = make_tenant()
    users_before = db_scalar("SELECT COUNT(*) FROM users WHERE business_id = :b", {"b": t.business_id})
    _send(http_client, admin, "POST", f"/businesses/{t.business_id}/suspend")
    assert db_scalar("SELECT COUNT(*) FROM users WHERE business_id = :b", {"b": t.business_id}) == users_before
    assert db_scalar("SELECT deleted_at FROM businesses WHERE id = :b", {"b": t.business_id}) is None


# ── plans ─────────────────────────────────────────────────────────────────────


def test_plans_crud(http_client, admin, make_tenant):
    name = f"Gold-{uuid.uuid4().hex[:6]}"
    c = _send(http_client, admin, "POST", "/plans", {"name": name, "monthly_price_pkr": "4500", "onetime_price_pkr": 90000, "notes": "x"})
    assert c.status_code == 201, c.text
    plan = c.json()
    assert set(plan) == {"id", "name", "monthly_price_pkr", "onetime_price_pkr", "notes", "is_active", "business_count"}
    assert plan["monthly_price_pkr"] == 4500 and plan["onetime_price_pkr"] == 90000 and plan["is_active"] is True
    assert not ({"features", "limits", "max_branches", "max_users"} & set(plan))  # a price tier only

    assert _send(http_client, admin, "POST", "/plans", {"name": name.upper(), "monthly_price_pkr": 1}).status_code in (201, 409)
    assert _send(http_client, admin, "POST", "/plans", {"name": name, "monthly_price_pkr": 1}).status_code == 409
    assert _send(http_client, admin, "POST", "/plans", {"name": "Neg", "monthly_price_pkr": -1}).status_code == 422

    u = _send(http_client, admin, "PUT", f"/plans/{plan['id']}", {"name": name, "monthly_price_pkr": 5000, "onetime_price_pkr": 0})
    assert u.status_code == 200 and u.json()["monthly_price_pkr"] == 5000
    assert audit_rows("plan.update", entity_id=plan["id"])[0]["before"]["monthly_price_pkr"] == "4500.00"

    d = _send(http_client, admin, "POST", f"/plans/{plan['id']}/disable")
    assert d.json()["is_active"] is False
    t = make_tenant()
    assert _send(http_client, admin, "PATCH", f"/businesses/{t.business_id}/subscription", {"plan_id": plan["id"]}).status_code == 400
    assert _send(http_client, admin, "POST", f"/plans/{plan['id']}/enable").json()["is_active"] is True
    _send(http_client, admin, "PATCH", f"/businesses/{t.business_id}/subscription", {"plan_id": plan["id"]})

    listed = next(p for p in _get(http_client, admin, "/plans").json() if p["id"] == plan["id"])
    assert listed["business_count"] == 1
    assert _send(http_client, admin, "DELETE", f"/plans/{plan['id']}").status_code == 409  # in use

    free = _send(http_client, admin, "POST", "/plans", {"name": f"Tmp-{uuid.uuid4().hex[:6]}", "monthly_price_pkr": 1}).json()
    assert _send(http_client, admin, "DELETE", f"/plans/{free['id']}").status_code == 204
    assert _send(http_client, admin, "PUT", f"/plans/{free['id']}", {"name": "x", "monthly_price_pkr": 1}).status_code == 404
    for action in ("plan.create", "plan.disable", "plan.enable", "plan.delete"):
        assert audit_rows(action), action


def test_default_plans_exist(http_client, admin):
    names = {p["name"] for p in _get(http_client, admin, "/plans").json()}
    assert {"Basic", "Multi-branch"} <= names


# ── settings ──────────────────────────────────────────────────────────────────


def test_settings_update_reaches_tenants_and_is_audited(http_client, admin, make_tenant, settings_restored):
    cur = _get(http_client, admin, "/settings").json()
    assert set(cur) == {"contact_phone", "contact_whatsapp", "contact_email", "payment_instructions"}
    new = {"contact_phone": "0321-1111111", "contact_whatsapp": "0321-2222222",
           "contact_email": "owner@pakpos.test", "payment_instructions": "JazzCash 0300-5555555\nBank: HBL 1234"}
    put = _send(http_client, admin, "PUT", "/settings", new)
    assert put.status_code == 200 and put.json() == new
    assert _get(http_client, admin, "/settings").json() == new

    t = make_tenant()
    st = http_client.get(t.url("/subscription/status"), headers=t.owner_headers).json()
    assert st["contact"] == {"phone": "0321-1111111", "whatsapp": "0321-2222222", "email": "owner@pakpos.test"}
    assert st["payment_instructions"] == new["payment_instructions"]

    row = audit_rows("settings.update")[-1]
    assert row["before"]["contact_phone"] == cur["contact_phone"] and row["after"]["contact_phone"] == "0321-1111111"
    assert _send(http_client, admin, "PUT", "/settings", {**new, "payment_instructions": "x" * 6000}).status_code == 422


# ── platform admin management ─────────────────────────────────────────────────


def test_create_admin_returns_2fa_secret_once_and_they_can_sign_in(http_client, admin, root):
    email = f"New-{uuid.uuid4().hex[:8]}@PakPOS.pk"
    r = _send(http_client, admin, "POST", "/admins", {"email": email, "name": "Helper", "password": "Another-Strong-Pass-9"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"admin", "totp_secret", "provisioning_uri"}
    assert body["provisioning_uri"].startswith("otpauth://totp/") and body["totp_secret"] in body["provisioning_uri"]
    assert body["admin"]["email"] == email.lower()

    from tests.helpers.platform import AdminCtx
    helper = AdminCtx(id=body["admin"]["id"], email=email.lower(), password="Another-Strong-Pass-9",
                      totp_secret=body["totp_secret"], root=root)
    assert login_response(http_client, helper).status_code == 200

    listing = _get(http_client, admin, "/admins").json()
    assert helper.id in {a["id"] for a in listing}
    assert not any(k in str(listing) for k in ("totp_secret", "password_hash", body["totp_secret"]))
    created = audit_rows("admin.create", entity_id=helper.id)[0]
    assert body["totp_secret"] not in str(created) and created["after"]["email"] == email.lower()

    assert _send(http_client, admin, "POST", "/admins", {"email": email.lower(), "name": "Dup", "password": "Another-Strong-Pass-9"}).status_code == 409
    assert _send(http_client, admin, "POST", "/admins", {"email": "x@y.pk", "name": "Short", "password": "short"}).status_code == 422
    assert _send(http_client, admin, "POST", "/admins", {"email": "not-an-email", "name": "x", "password": "Another-Strong-Pass-9"}).status_code == 422


def test_disable_signs_the_admin_out_everywhere_and_enable_restores(http_client, admin, root):
    victim = new_admin(http_client, root)
    assert http_client.get(victim.url("/me"), headers=victim.headers).status_code == 200
    r = _send(http_client, admin, "POST", f"/admins/{victim.id}/disable")
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert http_client.get(victim.url("/me"), headers=victim.headers).status_code == 401  # live token dies
    assert login_response(http_client, victim).status_code == 401
    assert _send(http_client, admin, "POST", f"/admins/{victim.id}/enable").json()["is_active"] is True
    assert http_client.get(victim.url("/me"), headers=victim.headers).status_code == 401  # old token stays dead
    assert login_response(http_client, victim).status_code == 200
    assert audit_rows("admin.disable", entity_id=victim.id)[0]["before"] == {"is_active": True}


def test_cannot_disable_yourself_or_a_missing_admin(http_client, admin):
    assert _send(http_client, admin, "POST", f"/admins/{admin.id}/disable").status_code == 400
    assert _send(http_client, admin, "POST", f"/admins/{uuid.uuid4()}/disable").status_code == 404
    assert http_client.get(admin.url("/me"), headers=admin.headers).status_code == 200


def test_reset_2fa_replaces_the_secret_and_ends_sessions(http_client, admin, root):
    victim = new_admin(http_client, root)
    old_secret = victim.totp_secret
    r = _send(http_client, admin, "POST", f"/admins/{victim.id}/reset-2fa")
    assert r.status_code == 200 and set(r.json()) == {"totp_secret", "provisioning_uri"}
    new_secret = r.json()["totp_secret"]
    assert new_secret != old_secret
    assert http_client.get(victim.url("/me"), headers=victim.headers).status_code == 401

    assert login_response(http_client, victim).status_code == 401  # code from the old secret
    victim.totp_secret = new_secret
    assert login_response(http_client, victim).status_code == 200
    assert new_secret not in str(audit_rows("admin.reset_2fa", entity_id=victim.id))


# ── audit log API ─────────────────────────────────────────────────────────────


def test_audit_list_shape_filters_and_paging(http_client, admin, make_tenant):
    t = make_tenant()
    _send(http_client, admin, "PATCH", f"/businesses/{t.business_id}/subscription", {"extend_days": 2})
    _send(http_client, admin, "POST", f"/businesses/{t.business_id}/suspend", {"reason": "r"})

    page = _get(http_client, admin, "/audit", business_id=t.business_id).json()
    assert set(page) == {"items", "total", "page", "page_size"} and page["total"] == 2
    item = page["items"][0]
    assert set(item) == {
        "id", "created_at", "admin_id", "admin_email", "action", "entity_type", "entity_id",
        "entity_label", "business_id", "before", "after", "ip_address",
    }
    assert item["action"] == "business.suspend" and item["admin_email"] == admin.email  # newest first
    assert item["before"]["status"] == "trial" and item["after"]["status"] == "suspended"

    by_action = _get(http_client, admin, "/audit", business_id=t.business_id, action="subscription_update").json()
    assert [i["action"] for i in by_action["items"]] == ["business.subscription_update"]
    by_admin = _get(http_client, admin, "/audit", admin=admin.email.split("@")[0]).json()
    assert by_admin["total"] >= 2 and all(i["admin_email"] == admin.email for i in by_admin["items"])
    by_type = _get(http_client, admin, "/audit", entity_type="business", business_id=t.business_id).json()
    assert by_type["total"] == 2
    today = datetime.now(timezone.utc).date().isoformat()
    assert _get(http_client, admin, "/audit", business_id=t.business_id, date_from=today, date_to=today).json()["total"] == 2
    assert _get(http_client, admin, "/audit", business_id=t.business_id, date_from="2999-01-01").json()["total"] == 0
    p1 = _get(http_client, admin, "/audit", business_id=t.business_id, page_size=1, page=1).json()
    p2 = _get(http_client, admin, "/audit", business_id=t.business_id, page_size=1, page=2).json()
    assert p1["items"][0]["id"] != p2["items"][0]["id"] and p1["total"] == 2


def test_audit_log_has_no_update_or_delete_endpoint(http_client, admin):
    for method in ("PUT", "PATCH", "DELETE", "POST"):
        r = _send(http_client, admin, method, "/audit", {})
        assert r.status_code in (404, 405)


def test_every_mutation_leaves_an_audit_row_with_the_actor(http_client, admin, make_tenant, settings_restored):
    t = make_tenant()
    ids = []
    before_count = db_scalar("SELECT COUNT(*) FROM platform_audit_log WHERE admin_id = :a", {"a": admin.id})
    calls = [
        ("PATCH", f"/businesses/{t.business_id}/subscription", {"extend_days": 1}),
        ("POST", f"/businesses/{t.business_id}/payments", {**PAY, "reference": uuid.uuid4().hex}),
        ("POST", f"/businesses/{t.business_id}/suspend", None),
        ("POST", f"/businesses/{t.business_id}/activate", None),
        ("POST", f"/businesses/{t.business_id}/notes", {"text": "n"}),
        ("PUT", "/settings", {"contact_phone": "1", "contact_whatsapp": "", "contact_email": "", "payment_instructions": ""}),
        ("POST", "/plans", {"name": f"A-{uuid.uuid4().hex[:6]}", "monthly_price_pkr": 1}),
    ]
    for method, path, body in calls:
        r = _send(http_client, admin, method, path, body)
        assert r.status_code in (200, 201), (path, r.text)
        ids.append(r)
    after_count = db_scalar("SELECT COUNT(*) FROM platform_audit_log WHERE admin_id = :a", {"a": admin.id})
    assert after_count - before_count == len(calls)
    assert db_scalar("SELECT COUNT(*) FROM platform_audit_log WHERE admin_email = 'unknown'") == 0
