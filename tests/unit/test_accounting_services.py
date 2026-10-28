"""Unit tests for accounting service helpers."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.accounting_coa_seed_service import DEFAULT_CHART_ACCOUNTS
from app.services.accounting_journal_service import _assert_balanced_for_post, _line_totals


def _line(*, debit: str = "0", credit: str = "0") -> SimpleNamespace:
    return SimpleNamespace(
        debit_amount=Decimal(debit),
        credit_amount=Decimal(credit),
    )


def test_default_chart_accounts_has_fourteen_unique_codes() -> None:
    codes = [row["account_code"] for row in DEFAULT_CHART_ACCOUNTS]
    assert len(codes) == 14
    assert len(set(codes)) == 14


def test_line_totals_sums_debits_and_credits() -> None:
    lines = [_line(debit="100.00"), _line(credit="40.00"), _line(credit="60.00")]
    total_debit, total_credit = _line_totals(lines)
    assert total_debit == Decimal("100.00")
    assert total_credit == Decimal("100.00")


def test_assert_balanced_for_post_accepts_balanced_entry() -> None:
    lines = [_line(debit="50.00"), _line(credit="50.00")]
    _assert_balanced_for_post(lines)


def test_assert_balanced_for_post_rejects_unbalanced_entry() -> None:
    lines = [_line(debit="50.00"), _line(credit="49.99")]
    with pytest.raises(HTTPException) as exc_info:
        _assert_balanced_for_post(lines)
    assert exc_info.value.status_code == 400
    assert "balanced" in exc_info.value.detail.lower()


def test_assert_balanced_for_post_rejects_single_line() -> None:
    with pytest.raises(HTTPException) as exc_info:
        _assert_balanced_for_post([_line(debit="10.00")])
    assert exc_info.value.status_code == 400
    assert "two lines" in exc_info.value.detail.lower()


def test_assert_balanced_for_post_rejects_unequal_totals_after_quantize() -> None:
    lines = [_line(debit="10.01"), _line(credit="10.00")]
    with pytest.raises(HTTPException):
        _assert_balanced_for_post(lines)


def test_seed_default_chart_is_idempotent_when_chart_exists() -> None:
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.services.accounting_coa_seed_service import seed_default_chart_of_accounts

    business_id = uuid4()
    db = AsyncMock()
    with patch(
        "app.services.accounting_coa_seed_service.chart_of_accounts_exists",
        new=AsyncMock(return_value=True),
    ):
        inserted = asyncio.run(seed_default_chart_of_accounts(db, business_id))
    assert inserted == 0
    db.add.assert_not_called()
