"""Accounting module RBAC and feature-flag smoke tests."""

from __future__ import annotations

import httpx
import pytest

from app.services.role_permission_seed import (
    ACCOUNTING_MANAGER_VIEW_KEYS,
    ACCOUNTING_PERMISSION_KEYS,
)
from tests.helpers.assertions import assert_status
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


def _balanced_journal_payload(account_ids: dict[str, str]) -> dict:
    return {
        "lines": [
            {
                "account_id": account_ids["1000"],
                "debit_amount": "5.00",
                "credit_amount": "0",
            },
            {
                "account_id": account_ids["4000"],
                "debit_amount": "0",
                "credit_amount": "5.00",
            },
        ]
    }


def test_accounting_feature_flag_blocks_owner_when_disabled(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Acct Flag {unique_suffix}",
        owner_name="Accounting Owner",
        phone_prefix="750",
    )

    blocked = http_client.get(
        f"{api_base}/accounting/coa",
        headers=tenant.owner_headers,
    )
    assert_status(blocked, 403, label="GET coa without flag")
    assert blocked.json()["detail"] == "Feature not enabled: enable_accounting"

    journal_blocked = http_client.get(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.owner_headers,
    )
    assert_status(journal_blocked, 403, label="GET journal without flag")


def test_accounting_permission_matrix_when_enabled(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Acct Matrix {unique_suffix}",
        owner_name="Accounting Owner",
        phone_prefix="751",
    )
    _enable_accounting(http_client, api_base, tenant.owner_headers)

    coa_resp = http_client.get(
        f"{api_base}/accounting/coa",
        headers=tenant.owner_headers,
    )
    assert_status(coa_resp, 200, label="GET coa seed")
    account_ids = {
        account["account_code"]: account["id"] for account in coa_resp.json()
    }

    assert tenant.manager_headers is not None
    assert tenant.cashier_headers is not None

    read_cases = [
        ("GET /accounting/coa (manager)", tenant.manager_headers, f"{api_base}/accounting/coa", 200),
        ("GET /accounting/coa (cashier)", tenant.cashier_headers, f"{api_base}/accounting/coa", 403),
        (
            "GET /accounting/journal-entries (manager)",
            tenant.manager_headers,
            f"{api_base}/accounting/journal-entries",
            200,
        ),
        (
            "GET /accounting/journal-entries (cashier)",
            tenant.cashier_headers,
            f"{api_base}/accounting/journal-entries",
            403,
        ),
    ]
    for label, headers, url, expected in read_cases:
        assert_status(http_client.get(url, headers=headers), expected, label=label)

    owner_create = http_client.post(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.owner_headers,
        json=_balanced_journal_payload(account_ids),
    )
    assert_status(owner_create, 201, label="owner POST journal entry")
    entry_id = owner_create.json()["id"]

    write_denied_cases = [
        (
            "POST /accounting/coa (manager)",
            tenant.manager_headers,
            "post",
            f"{api_base}/accounting/coa",
            {
                "account_code": "7998",
                "account_name": "Blocked",
                "account_type": "expense",
            },
        ),
        (
            "POST /accounting/journal-entries (manager)",
            tenant.manager_headers,
            "post",
            f"{api_base}/accounting/journal-entries",
            _balanced_journal_payload(account_ids),
        ),
        (
            "POST /accounting/journal-entries/{id}/post (manager)",
            tenant.manager_headers,
            "post",
            f"{api_base}/accounting/journal-entries/{entry_id}/post",
            None,
        ),
        (
            "POST /accounting/coa (cashier)",
            tenant.cashier_headers,
            "post",
            f"{api_base}/accounting/coa",
            {
                "account_code": "7997",
                "account_name": "Blocked Cashier",
                "account_type": "expense",
            },
        ),
    ]
    for label, headers, method, url, body in write_denied_cases:
        if method == "post":
            response = http_client.post(url, headers=headers, json=body)
        else:
            response = http_client.request(method, url, headers=headers, json=body)
        assert_status(response, 403, label=label)

    owner_post = http_client.post(
        f"{api_base}/accounting/journal-entries/{entry_id}/post",
        headers=tenant.owner_headers,
    )
    assert_status(owner_post, 200, label="owner POST post journal entry")


def test_accounting_role_permission_keys_match_catalog(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=f"Acct Catalog {unique_suffix}",
        owner_name="Accounting Owner",
        phone_prefix="752",
    )

    roles_response = http_client.get(
        f"{api_base}/roles",
        headers=tenant.owner_headers,
    )
    assert_status(roles_response, 200, label="GET /roles")
    roles = {role["name"].lower(): role for role in roles_response.json()}

    owner_keys = set(roles["owner"]["permission_keys"])
    manager_keys = set(roles["manager"]["permission_keys"])
    cashier_keys = set(roles["cashier"]["permission_keys"])

    assert ACCOUNTING_PERMISSION_KEYS.issubset(owner_keys)
    assert manager_keys & ACCOUNTING_PERMISSION_KEYS == ACCOUNTING_MANAGER_VIEW_KEYS
    assert ACCOUNTING_PERMISSION_KEYS.isdisjoint(cashier_keys)

    permissions_response = http_client.get(
        f"{api_base}/permissions",
        headers=tenant.owner_headers,
    )
    assert_status(permissions_response, 200, label="GET /permissions")
    accounting_module = next(
        (
            group
            for group in permissions_response.json()["modules"]
            if group["module"] == "accounting"
        ),
        None,
    )
    assert accounting_module is not None
    catalog_keys = {
        item["permission_key"] for item in accounting_module["permissions"]
    }
    assert catalog_keys == set(ACCOUNTING_PERMISSION_KEYS)
