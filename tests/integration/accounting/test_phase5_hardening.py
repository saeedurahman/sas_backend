"""Phase 5 — Accounting edge-case guards."""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException

from app.database import AsyncSessionLocal
from app.schemas.accounting import UpdateChartOfAccountRequest
from app.services.accounting_coa_service import update_chart_of_account
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


def test_coa_parent_cycle_rejected(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_accounting_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="740",
        business_name=f"CoA Cycle {unique_suffix}",
    )
    suffix = tenant.suffix
    accounts = _get_account_ids(
        http_client,
        api_base,
        tenant.owner_headers,
        "1000",
    )
    cash_id = accounts["1000"]

    child_resp = http_client.post(
        f"{api_base}/accounting/coa",
        headers=tenant.owner_headers,
        json={
            "account_code": f"710{suffix % 1000}",
            "account_name": f"Child {suffix}",
            "account_type": "asset",
            "account_subtype": "other",
            "parent_id": cash_id,
        },
    )
    assert_status(child_resp, 201, label="POST child account")
    child_id = child_resp.json()["id"]

    grandchild_resp = http_client.post(
        f"{api_base}/accounting/coa",
        headers=tenant.owner_headers,
        json={
            "account_code": f"711{suffix % 1000}",
            "account_name": f"Grandchild {suffix}",
            "account_type": "asset",
            "account_subtype": "other",
            "parent_id": child_id,
        },
    )
    assert_status(grandchild_resp, 201, label="POST grandchild account")
    grandchild_id = grandchild_resp.json()["id"]

    me = http_client.get(f"{api_base}/auth/me", headers=tenant.owner_headers)
    assert_status(me, 200, label="GET /auth/me")
    owner_id = UUID(me.json()["id"])

    async def attempt_cycle_update() -> HTTPException | None:
        async with AsyncSessionLocal() as db:
            try:
                await update_chart_of_account(
                    db,
                    UUID(cash_id),
                    UUID(tenant.business_id),
                    UpdateChartOfAccountRequest(parent_id=UUID(grandchild_id)),
                    owner_id,
                )
            except HTTPException as exc:
                return exc
        return None

    error = asyncio.run(attempt_cycle_update())
    assert error is not None
    assert error.status_code == 400
    assert "cycle" in error.detail.lower()


def test_post_rejects_inactive_account_on_draft_lines(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_accounting_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="741",
        business_name=f"Inactive Post {unique_suffix}",
    )
    suffix = tenant.suffix
    accounts = _get_account_ids(
        http_client,
        api_base,
        tenant.owner_headers,
        "1000",
        "4000",
    )

    custom_resp = http_client.post(
        f"{api_base}/accounting/coa",
        headers=tenant.owner_headers,
        json={
            "account_code": f"712{suffix % 1000}",
            "account_name": f"Temp Asset {suffix}",
            "account_type": "asset",
            "account_subtype": "other",
        },
    )
    assert_status(custom_resp, 201, label="POST custom account")
    custom_id = custom_resp.json()["id"]

    draft_resp = http_client.post(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.owner_headers,
        json={
            "lines": [
                {
                    "account_id": custom_id,
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
    assert_status(draft_resp, 201, label="POST draft with custom account")
    entry_id = draft_resp.json()["id"]

    deactivate_resp = http_client.put(
        f"{api_base}/accounting/coa/{custom_id}",
        headers=tenant.owner_headers,
        json={"is_active": False},
    )
    assert_status(deactivate_resp, 200, label="deactivate custom account")

    post_resp = http_client.post(
        f"{api_base}/accounting/journal-entries/{entry_id}/post",
        headers=tenant.owner_headers,
    )
    assert_status(post_resp, 400, label="POST with inactive account line")
    assert "inactive" in post_resp.json()["detail"].lower()


def test_journal_list_pagination(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    tenant = _setup_accounting_tenant(
        http_client,
        api_base,
        unique_suffix,
        phone_prefix="742",
        business_name=f"Journal Page {unique_suffix}",
    )
    accounts = _get_account_ids(
        http_client,
        api_base,
        tenant.owner_headers,
        "1000",
        "4000",
    )
    lines = [
        {
            "account_id": accounts["1000"],
            "debit_amount": "10.00",
            "credit_amount": "0",
        },
        {
            "account_id": accounts["4000"],
            "debit_amount": "0",
            "credit_amount": "10.00",
        },
    ]

    for index in range(3):
        create_resp = http_client.post(
            f"{api_base}/accounting/journal-entries",
            headers=tenant.owner_headers,
            json={"description": f"Entry {index}", "lines": lines},
        )
        assert_status(create_resp, 201, label=f"POST journal entry {index}")

    page_one = http_client.get(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.owner_headers,
        params={"skip": 0, "limit": 2},
    )
    assert_status(page_one, 200, label="GET journal entries page 1")
    assert len(page_one.json()) == 2

    page_two = http_client.get(
        f"{api_base}/accounting/journal-entries",
        headers=tenant.owner_headers,
        params={"skip": 2, "limit": 2},
    )
    assert_status(page_two, 200, label="GET journal entries page 2")
    assert len(page_two.json()) >= 1
