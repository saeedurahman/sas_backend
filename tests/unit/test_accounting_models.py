"""Model import smoke tests for accounting foundation."""

from __future__ import annotations


def test_accounting_models_import_cleanly() -> None:
    from app.models import (  # noqa: F401
        ChartOfAccount,
        JournalEntry,
        JournalLine,
    )
    from app.models.enums import (  # noqa: F401
        AccountSubtypeEnum,
        AccountTypeEnum,
        JournalEntryStatusEnum,
    )

    assert ChartOfAccount.__tablename__ == "chart_of_accounts"
    assert JournalEntry.__tablename__ == "journal_entries"
    assert JournalLine.__tablename__ == "journal_lines"

    assert AccountTypeEnum.asset.value == "asset"
    assert AccountTypeEnum.liability.value == "liability"
    assert AccountTypeEnum.equity.value == "equity"
    assert AccountTypeEnum.income.value == "income"
    assert AccountTypeEnum.expense.value == "expense"

    assert AccountSubtypeEnum.cash.value == "cash"
    assert AccountSubtypeEnum.sales_revenue.value == "sales_revenue"

    assert JournalEntryStatusEnum.draft.value == "draft"
    assert JournalEntryStatusEnum.posted.value == "posted"
    assert JournalEntryStatusEnum.voided.value == "voided"


def test_chart_of_accounts_unique_code_constraint() -> None:
    from app.models.accounting import ChartOfAccount

    constraint_names = {
        c.name for c in ChartOfAccount.__table__.constraints if c.name
    }
    assert "uq_coa_code" in constraint_names


def test_journal_entry_requires_created_by() -> None:
    from app.models.accounting import JournalEntry, JournalLine

    assert JournalEntry.__table__.c.created_by.nullable is False
    assert JournalLine.__table__.c.created_by.nullable is False


def test_journal_line_amount_columns() -> None:
    from app.models.accounting import JournalLine

    assert JournalLine.__table__.c.debit_amount.nullable is False
    assert JournalLine.__table__.c.credit_amount.nullable is False
