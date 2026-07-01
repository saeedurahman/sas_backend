"""Phase 2 — Chart of accounts seed, CRUD API, and permissions."""

from __future__ import annotations

import httpx
import pytest

from app.services.accounting_coa_seed_service import DEFAULT_CHART_ACCOUNTS
from tests.helpers.assertions import assert_status
from tests.helpers.db import db_scalar
from tests.helpers.tenants import build_rbac_tenant

pytestmark = pytest.mark.integration


def _enable_accounting(
    client: httpx.Client,
    api_base: str,
    headers: dict[str, str],
) -> None:
    response = client.put(
        f"{api_base}/business/config",
        headers=headers,
        json={"config_json": {"enable_accounting": True}},
    )
    assert_status(response, 200, label="enable enable_accounting")


def _setup_accounting_tenant(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
    *,
    phone_prefix: str,
    business_name: str,
    enable: bool = True,
) -> object:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=business_name,
        owner_name="Accounting Owner",
        phone_prefix=phone_prefix,
    )
    if enable:
        _enable_accounting(http_client, api_base, tenant.owner_headers)
    return tenant


def test_coa_seeds_on_enable_and_list(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_accounting_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="720",
        business_name=f"CoA Seed {unique_suffix}",
    )

    count = db_scalar(
        """
        SELECT COUNT(*)
        FROM chart_of_accounts
        WHERE business_id = :business_id
          AND deleted_at IS NULL
        """,
        {"business_id": tenant.business_id},
    )
    assert count == len(DEFAULT_CHART_ACCOUNTS)

    list_resp = http_client.get(
        f"{api_base}/accounting/coa",
        headers=tenant.owner_headers,
    )
    assert_status(list_resp, 200, label="GET /accounting/coa")
    accounts = list_resp.json()
    assert len(accounts) == len(DEFAULT_CHART_ACCOUNTS)
    assert all(account["is_system"] for account in accounts)
    codes = {account["account_code"] for account in accounts}
    assert codes == {row["account_code"] for row in DEFAULT_CHART_ACCOUNTS}


def test_coa_crud_and_guards(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_accounting_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="721",
        business_name=f"CoA CRUD {unique_suffix}",
    )
    suffix = tenant.suffix

    list_resp = http_client.get(
        f"{api_base}/accounting/coa",
        headers=tenant.owner_headers,
    )
    assert_status(list_resp, 200, label="GET /accounting/coa baseline")
    cash_account = next(
        account for account in list_resp.json() if account["account_code"] == "1000"
    )

    create_resp = http_client.post(
        f"{api_base}/accounting/coa",
        headers=tenant.owner_headers,
        json={
            "account_code": f"700{suffix % 1000}",
            "account_name": f"Misc Expense {suffix}",
            "account_type": "expense",
            "account_subtype": "other",
            "parent_id": cash_account["id"],
            "description": "Custom expense account",
        },
    )
    assert_status(create_resp, 201, label="POST /accounting/coa")
    created = create_resp.json()
    assert created["is_system"] is False
    assert created["parent_id"] == cash_account["id"]

    get_resp = http_client.get(
        f"{api_base}/accounting/coa/{created['id']}",
        headers=tenant.owner_headers,
    )
    assert_status(get_resp, 200, label="GET /accounting/coa/{id}")
    assert get_resp.json()["account_name"] == f"Misc Expense {suffix}"

    update_resp = http_client.put(
        f"{api_base}/accounting/coa/{created['id']}",
        headers=tenant.owner_headers,
        json={"account_name": f"Updated Expense {suffix}", "is_active": False},
    )
    assert_status(update_resp, 200, label="PUT /accounting/coa/{id}")
    assert update_resp.json()["account_name"] == f"Updated Expense {suffix}"
    assert update_resp.json()["is_active"] is False

    active_resp = http_client.get(
        f"{api_base}/accounting/coa",
        headers=tenant.owner_headers,
        params={"active_only": True},
    )
    assert_status(active_resp, 200, label="GET /accounting/coa active_only")
    active_ids = {account["id"] for account in active_resp.json()}
    assert created["id"] not in active_ids

    tree_resp = http_client.get(
        f"{api_base}/accounting/coa",
        headers=tenant.owner_headers,
        params={"tree": True},
    )
    assert_status(tree_resp, 200, label="GET /accounting/coa tree")
    cash_node = next(
        node for node in tree_resp.json() if node["account_code"] == "1000"
    )
    child_ids = {child["id"] for child in cash_node["children"]}
    assert created["id"] in child_ids

    dup_resp = http_client.post(
        f"{api_base}/accounting/coa",
        headers=tenant.owner_headers,
        json={
            "account_code": created["account_code"],
            "account_name": "Duplicate",
            "account_type": "expense",
        },
    )
    assert_status(dup_resp, 409, label="POST duplicate account_code")

    system_code_update = http_client.put(
        f"{api_base}/accounting/coa/{cash_account['id']}",
        headers=tenant.owner_headers,
        json={"account_code": "9999"},
    )
    assert_status(system_code_update, 400, label="PUT system account_code")

    system_delete = http_client.delete(
        f"{api_base}/accounting/coa/{cash_account['id']}",
        headers=tenant.owner_headers,
    )
    assert_status(system_delete, 400, label="DELETE system account")

    parent_delete = http_client.delete(
        f"{api_base}/accounting/coa/{cash_account['id']}",
        headers=tenant.owner_headers,
    )
    assert_status(parent_delete, 400, label="DELETE account with children")

    delete_resp = http_client.delete(
        f"{api_base}/accounting/coa/{created['id']}",
        headers=tenant.owner_headers,
    )
    assert_status(delete_resp, 200, label="DELETE /accounting/coa/{id}")
    assert delete_resp.json()["message"] == "Chart of accounts entry deleted"

    gone_resp = http_client.get(
        f"{api_base}/accounting/coa/{created['id']}",
        headers=tenant.owner_headers,
    )
    assert_status(gone_resp, 404, label="GET deleted account")


def test_coa_feature_flag_and_permissions(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_accounting_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="722",
        business_name=f"CoA Perms {unique_suffix}",
        enable=False,
    )

    blocked = http_client.get(
        f"{api_base}/accounting/coa",
        headers=tenant.owner_headers,
    )
    assert_status(blocked, 403, label="GET coa without flag")
    assert blocked.json()["detail"] == "Feature not enabled: enable_accounting"

    _enable_accounting(http_client, api_base, tenant.owner_headers)

    assert tenant.manager_headers is not None
    manager_view = http_client.get(
        f"{api_base}/accounting/coa",
        headers=tenant.manager_headers,
    )
    assert_status(manager_view, 200, label="manager GET coa")

    manager_create = http_client.post(
        f"{api_base}/accounting/coa",
        headers=tenant.manager_headers,
        json={
            "account_code": "7999",
            "account_name": "Manager Blocked",
            "account_type": "expense",
        },
    )
    assert_status(manager_create, 403, label="manager POST coa")

    assert tenant.cashier_headers is not None
    cashier_view = http_client.get(
        f"{api_base}/accounting/coa",
        headers=tenant.cashier_headers,
    )
    assert_status(cashier_view, 403, label="cashier GET coa")
