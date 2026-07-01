"""Phase 3 — Journal entry draft CRUD and permissions."""

from __future__ import annotations

import httpx
import pytest

from tests.helpers.assertions import assert_status
from tests.helpers.db import db_execute
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
) -> object:
    tenant = build_rbac_tenant(
        http_client,
        api_base,
        unique_suffix,
        business_name=business_name,
        owner_name="Accounting Owner",
        phone_prefix=phone_prefix,
    )
    _enable_accounting(http_client, api_base, tenant.owner_headers)
    return tenant


def _get_account_ids(
    http_client: httpx.Client,
    api_base: str,
    headers: dict[str, str],
    *codes: str,
) -> dict[str, str]:
    response = http_client.get(f"{api_base}/accounting/coa", headers=headers)
    assert_status(response, 200, label="GET /accounting/coa for account ids")
    by_code = {account["account_code"]: account["id"] for account in response.json()}
    return {code: by_code[code] for code in codes}


def test_journal_draft_crud_balanced_and_unbalanced(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_accounting_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="730",
        business_name=f"Journal CRUD {unique_suffix}",
    )
    accounts = _get_account_ids(
        http_client,
        api_base,
        tenant.owner_headers,
        "1000",
        "4000",
        "6100",
    )

    unbalanced_resp = http_client.post(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.owner_headers,
        json={
            "description": "Unbalanced rent accrual",
            "branch_id": tenant.branch_id,
            "lines": [
                {
                    "account_id": accounts["6100"],
                    "debit_amount": "250.00",
                    "credit_amount": "0",
                    "line_order": 1,
                },
                {
                    "account_id": accounts["1000"],
                    "debit_amount": "50.00",
                    "credit_amount": "0",
                    "line_order": 2,
                },
            ],
        },
    )
    assert_status(unbalanced_resp, 201, label="POST unbalanced draft")
    unbalanced = unbalanced_resp.json()
    assert unbalanced["status"] == "draft"
    assert unbalanced["entry_number"].startswith("JE-")
    assert unbalanced["total_debit"] == "300.00"
    assert unbalanced["total_credit"] == "0.00"
    assert len(unbalanced["lines"]) == 2

    balanced_resp = http_client.post(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.owner_headers,
        json={
            "description": "Cash sale adjustment",
            "lines": [
                {
                    "account_id": accounts["1000"],
                    "debit_amount": "100.00",
                    "credit_amount": "0",
                },
                {
                    "account_id": accounts["4000"],
                    "debit_amount": "0",
                    "credit_amount": "100.00",
                },
            ],
        },
    )
    assert_status(balanced_resp, 201, label="POST balanced draft")
    balanced = balanced_resp.json()
    assert balanced["total_debit"] == "100.00"
    assert balanced["total_credit"] == "100.00"

    list_resp = http_client.get(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.owner_headers,
        params={"status": "draft"},
    )
    assert_status(list_resp, 200, label="GET /accounting/journal-entries")
    assert len(list_resp.json()) >= 2

    get_resp = http_client.get(
        f"{api_base}/accounting/journal-entries/{balanced['id']}",
        headers=tenant.owner_headers,
    )
    assert_status(get_resp, 200, label="GET /accounting/journal-entries/{id}")
    assert get_resp.json()["description"] == "Cash sale adjustment"

    update_resp = http_client.put(
        f"{api_base}/accounting/journal-entries/{balanced['id']}",
        headers=tenant.owner_headers,
        json={
            "description": "Updated cash sale adjustment",
            "lines": [
                {
                    "account_id": accounts["1000"],
                    "debit_amount": "150.00",
                    "credit_amount": "0",
                },
                {
                    "account_id": accounts["4000"],
                    "debit_amount": "0",
                    "credit_amount": "150.00",
                },
            ],
        },
    )
    assert_status(update_resp, 200, label="PUT draft journal entry")
    updated = update_resp.json()
    assert updated["description"] == "Updated cash sale adjustment"
    assert updated["total_debit"] == "150.00"

    delete_resp = http_client.delete(
        f"{api_base}/accounting/journal-entries/{unbalanced['id']}",
        headers=tenant.owner_headers,
    )
    assert_status(delete_resp, 200, label="DELETE draft journal entry")
    assert delete_resp.json()["message"] == "Journal entry deleted"

    gone_resp = http_client.get(
        f"{api_base}/accounting/journal-entries/{unbalanced['id']}",
        headers=tenant.owner_headers,
    )
    assert_status(gone_resp, 404, label="GET deleted journal entry")


def test_journal_draft_validation_and_posted_guard(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_accounting_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="731",
        business_name=f"Journal Guards {unique_suffix}",
    )
    accounts = _get_account_ids(
        http_client,
        api_base,
        tenant.owner_headers,
        "1000",
        "4000",
    )

    invalid_account_resp = http_client.post(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.owner_headers,
        json={
            "lines": [
                {
                    "account_id": "00000000-0000-0000-0000-000000000099",
                    "debit_amount": "10.00",
                    "credit_amount": "0",
                }
            ]
        },
    )
    assert_status(invalid_account_resp, 400, label="POST invalid account")

    both_sides_resp = http_client.post(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.owner_headers,
        json={
            "lines": [
                {
                    "account_id": accounts["1000"],
                    "debit_amount": "10.00",
                    "credit_amount": "10.00",
                }
            ]
        },
    )
    assert_status(both_sides_resp, 422, label="POST line with debit and credit")

    create_resp = http_client.post(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.owner_headers,
        json={
            "lines": [
                {
                    "account_id": accounts["1000"],
                    "debit_amount": "25.00",
                    "credit_amount": "0",
                },
                {
                    "account_id": accounts["4000"],
                    "debit_amount": "0",
                    "credit_amount": "25.00",
                },
            ]
        },
    )
    assert_status(create_resp, 201, label="POST draft for posted guard")
    entry_id = create_resp.json()["id"]

    db_execute(
        """
        UPDATE journal_entries
        SET status = 'posted', posted_at = NOW()
        WHERE id = :entry_id
        """,
        {"entry_id": entry_id},
    )

    update_posted = http_client.put(
        f"{api_base}/accounting/journal-entries/{entry_id}",
        headers=tenant.owner_headers,
        json={"description": "Should fail"},
    )
    assert_status(update_posted, 400, label="PUT posted journal entry")
    assert "posted" in update_posted.json()["detail"].lower()

    delete_posted = http_client.delete(
        f"{api_base}/accounting/journal-entries/{entry_id}",
        headers=tenant.owner_headers,
    )
    assert_status(delete_posted, 400, label="DELETE posted journal entry")


def test_journal_permissions(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_accounting_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="732",
        business_name=f"Journal Perms {unique_suffix}",
    )
    accounts = _get_account_ids(
        http_client,
        api_base,
        tenant.owner_headers,
        "1000",
        "4000",
    )

    assert tenant.manager_headers is not None
    manager_view = http_client.get(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.manager_headers,
    )
    assert_status(manager_view, 200, label="manager GET journal entries")

    manager_create = http_client.post(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.manager_headers,
        json={
            "lines": [
                {
                    "account_id": accounts["1000"],
                    "debit_amount": "1.00",
                    "credit_amount": "0",
                }
            ]
        },
    )
    assert_status(manager_create, 403, label="manager POST journal entry")

    assert tenant.cashier_headers is not None
    cashier_view = http_client.get(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.cashier_headers,
    )
    assert_status(cashier_view, 403, label="cashier GET journal entries")


def _balanced_lines(
    accounts: dict[str, str],
    *,
    amount: str = "100.00",
) -> list[dict[str, str]]:
    return [
        {
            "account_id": accounts["1000"],
            "debit_amount": amount,
            "credit_amount": "0",
        },
        {
            "account_id": accounts["4000"],
            "debit_amount": "0",
            "credit_amount": amount,
        },
    ]


def test_journal_post_workflow_and_immutability(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_accounting_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="733",
        business_name=f"Journal Post {unique_suffix}",
    )
    accounts = _get_account_ids(
        http_client,
        api_base,
        tenant.owner_headers,
        "1000",
        "4000",
    )

    balanced_resp = http_client.post(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.owner_headers,
        json={"description": "Balanced entry", "lines": _balanced_lines(accounts)},
    )
    assert_status(balanced_resp, 201, label="POST balanced draft for post")
    entry_id = balanced_resp.json()["id"]

    post_resp = http_client.post(
        f"{api_base}/accounting/journal-entries/{entry_id}/post",
        headers=tenant.owner_headers,
    )
    assert_status(post_resp, 200, label="POST /journal-entries/{id}/post")
    posted = post_resp.json()
    assert posted["status"] == "posted"
    assert posted["posted_at"] is not None
    assert posted["total_debit"] == "100.00"
    assert posted["total_credit"] == "100.00"

    repost_resp = http_client.post(
        f"{api_base}/accounting/journal-entries/{entry_id}/post",
        headers=tenant.owner_headers,
    )
    assert_status(repost_resp, 400, label="POST already posted entry")

    update_posted = http_client.put(
        f"{api_base}/accounting/journal-entries/{entry_id}",
        headers=tenant.owner_headers,
        json={"description": "Should fail"},
    )
    assert_status(update_posted, 400, label="PUT posted journal entry after post")

    delete_posted = http_client.delete(
        f"{api_base}/accounting/journal-entries/{entry_id}",
        headers=tenant.owner_headers,
    )
    assert_status(delete_posted, 400, label="DELETE posted journal entry after post")

    unbalanced_resp = http_client.post(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.owner_headers,
        json={
            "lines": [
                {
                    "account_id": accounts["1000"],
                    "debit_amount": "75.00",
                    "credit_amount": "0",
                },
                {
                    "account_id": accounts["4000"],
                    "debit_amount": "25.00",
                    "credit_amount": "0",
                },
            ]
        },
    )
    assert_status(unbalanced_resp, 201, label="POST unbalanced draft for post reject")
    unbalanced_id = unbalanced_resp.json()["id"]

    post_unbalanced = http_client.post(
        f"{api_base}/accounting/journal-entries/{unbalanced_id}/post",
        headers=tenant.owner_headers,
    )
    assert_status(post_unbalanced, 400, label="POST unbalanced draft")
    assert "balanced" in post_unbalanced.json()["detail"].lower()

    single_line_resp = http_client.post(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.owner_headers,
        json={
            "lines": [
                {
                    "account_id": accounts["1000"],
                    "debit_amount": "10.00",
                    "credit_amount": "0",
                }
            ]
        },
    )
    assert_status(single_line_resp, 201, label="POST single-line draft")
    single_line_id = single_line_resp.json()["id"]

    post_single = http_client.post(
        f"{api_base}/accounting/journal-entries/{single_line_id}/post",
        headers=tenant.owner_headers,
    )
    assert_status(post_single, 400, label="POST single-line draft")
    assert "two lines" in post_single.json()["detail"].lower()


def test_journal_post_permissions_and_reversal_pattern(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_accounting_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="734",
        business_name=f"Journal Post Perms {unique_suffix}",
    )
    accounts = _get_account_ids(
        http_client,
        api_base,
        tenant.owner_headers,
        "1000",
        "4000",
    )

    original_resp = http_client.post(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.owner_headers,
        json={
            "description": "Original posted entry",
            "lines": _balanced_lines(accounts, amount="80.00"),
        },
    )
    assert_status(original_resp, 201, label="POST original entry")
    original_id = original_resp.json()["id"]

    assert tenant.manager_headers is not None
    manager_post = http_client.post(
        f"{api_base}/accounting/journal-entries/{original_id}/post",
        headers=tenant.manager_headers,
    )
    assert_status(manager_post, 403, label="manager POST post journal entry")

    owner_post = http_client.post(
        f"{api_base}/accounting/journal-entries/{original_id}/post",
        headers=tenant.owner_headers,
    )
    assert_status(owner_post, 200, label="owner POST post journal entry")

    reversal_resp = http_client.post(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.owner_headers,
        json={
            "description": "Manual reversal of original entry",
            "lines": [
                {
                    "account_id": accounts["1000"],
                    "debit_amount": "0",
                    "credit_amount": "80.00",
                },
                {
                    "account_id": accounts["4000"],
                    "debit_amount": "80.00",
                    "credit_amount": "0",
                },
            ],
        },
    )
    assert_status(reversal_resp, 201, label="POST reversal draft")
    reversal_id = reversal_resp.json()["id"]

    reversal_post = http_client.post(
        f"{api_base}/accounting/journal-entries/{reversal_id}/post",
        headers=tenant.owner_headers,
    )
    assert_status(reversal_post, 200, label="POST reversal entry")
    reversal = reversal_post.json()
    assert reversal["status"] == "posted"
    assert reversal["total_debit"] == "80.00"
    assert reversal["total_credit"] == "80.00"
