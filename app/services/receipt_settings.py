"""Numbering and batch-policy settings (``app_settings``, business-wide) and sale numbers.

Official sale numbers
---------------------
* ``sales.invoice_prefix`` (default ``INV``), ``sales.invoice_digits`` (default 4),
  ``sales.invoice_start`` (default 1).
* Neither digits nor start configured: the long-standing daily format ``PREFIX-YYYYMMDD-####``
  (the prefix is honoured). Existing numbers never change either way.
* Digits or start configured: one continuous counter per business, ``PREFIX-0001``, taking
  ``max(counter + 1, start)``. The numeric counter is stored in ``sales.sale_seq`` so numbers
  sort numerically even after they outgrow the digit width (text order would put 10000 before 9999).
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

KEY_INVOICE_PREFIX = "sales.invoice_prefix"
KEY_INVOICE_DIGITS = "sales.invoice_digits"
KEY_INVOICE_START = "sales.invoice_start"
KEY_NUMBER_PREFIX = "receipt.number_prefix"
KEY_PICK_POLICY = "inventory.pick_policy"
KEY_EXPIRED_SALE = "inventory.expired_sale"

DEFAULTS: dict[str, Any] = {
    KEY_INVOICE_PREFIX: "INV",
    KEY_INVOICE_DIGITS: 4,
    KEY_INVOICE_START: 1,
    KEY_NUMBER_PREFIX: "",
    KEY_PICK_POLICY: "fifo",
    KEY_EXPIRED_SALE: "warn",
}
SETTING_KEYS = frozenset(DEFAULTS)
PICK_POLICIES = ("fifo", "fefo")
EXPIRED_MODES = ("block", "warn", "allow")

_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_/]{0,9}$")
_NUMBER_PREFIX = re.compile(r"^[A-Za-z0-9]{1,6}$")


def normalize_value(key: str, value: Any) -> Any:
    if key == KEY_INVOICE_PREFIX:
        if not isinstance(value, str) or not _PREFIX.match(value.strip()):
            raise ValueError("must be 1-10 letters, digits, '-', '_' or '/'")
        return value.strip().upper()
    if key == KEY_INVOICE_DIGITS:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
            raise ValueError("must be a whole number from 1 to 10")
        return value
    if key == KEY_INVOICE_START:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 999_999_999:
            raise ValueError("must be a whole number of at least 1")
        return value
    if key == KEY_NUMBER_PREFIX:
        if value is None or value == "":
            return ""
        if not isinstance(value, str) or not _NUMBER_PREFIX.match(value.strip()):
            raise ValueError("must be empty or 1-6 letters/digits")
        return value.strip().upper()
    if key == KEY_PICK_POLICY:
        if value not in PICK_POLICIES:
            raise ValueError(f"must be one of: {', '.join(PICK_POLICIES)}")
        return value
    if key == KEY_EXPIRED_SALE:
        if value not in EXPIRED_MODES:
            raise ValueError(f"must be one of: {', '.join(EXPIRED_MODES)}")
        return value
    raise ValueError("unknown setting")


def validate_setting_payload(key: str, payload: dict[str, Any]) -> dict[str, Any]:
    if "value" not in payload:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{key}: setting_value must be {{\"value\": ...}}")
    try:
        return {"value": normalize_value(key, payload["value"])}
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{key}: {exc}") from None


async def load_raw(db: AsyncSession, business_id: UUID) -> dict[str, Any]:
    rows = (
        await db.execute(
            text(
                "SELECT setting_key, setting_value FROM app_settings "
                "WHERE business_id = :b AND branch_id IS NULL AND deleted_at IS NULL "
                "AND setting_key = ANY(CAST(:keys AS text[]))"
            ),
            {"b": business_id, "keys": sorted(SETTING_KEYS)},
        )
    ).all()
    out: dict[str, Any] = {}
    for key, raw in rows:
        value = raw.get("value") if isinstance(raw, dict) else raw
        try:
            out[key] = normalize_value(key, value)
        except ValueError:
            continue  # malformed rows fall back to the default
    return out


async def load_settings(db: AsyncSession, business_id: UUID) -> dict[str, Any]:
    return {**DEFAULTS, **await load_raw(db, business_id)}


async def inventory_policy(db: AsyncSession, business_id: UUID) -> tuple[str, str]:
    """(pick_policy, expired_sale)."""
    cfg = await load_settings(db, business_id)
    return cfg[KEY_PICK_POLICY], cfg[KEY_EXPIRED_SALE]


def format_number(prefix: str, seq: int, digits: int) -> str:
    body = f"{seq:0{digits}d}"
    return f"{prefix}-{body}" if prefix else body


async def generate_sale_number(db: AsyncSession, business_id: UUID) -> tuple[str, int | None]:
    """Next official sale number and (continuous mode) its numeric counter."""
    from app.services.stock_service import generate_document_number

    raw = await load_raw(db, business_id)
    prefix = raw.get(KEY_INVOICE_PREFIX, DEFAULTS[KEY_INVOICE_PREFIX])
    if KEY_INVOICE_DIGITS not in raw and KEY_INVOICE_START not in raw:
        # Daily format; the counter table keys on a <=10 char prefix.
        number = await generate_document_number(db, business_id, prefix, object, None)
        return number, None
    digits = raw.get(KEY_INVOICE_DIGITS, DEFAULTS[KEY_INVOICE_DIGITS])
    start = raw.get(KEY_INVOICE_START, DEFAULTS[KEY_INVOICE_START])
    for _ in range(100):
        seq = (
            await db.execute(
                text(
                    """
                    INSERT INTO document_number_counters (id, business_id, prefix, date_key, last_sequence)
                    VALUES (gen_random_uuid(), :b, '#SALE', 'SEQ', :start)
                    ON CONFLICT (business_id, prefix, date_key)
                    DO UPDATE SET last_sequence =
                        GREATEST(document_number_counters.last_sequence + 1, :start)
                    RETURNING last_sequence
                    """
                ),
                {"b": business_id, "start": start},
            )
        ).scalar_one()
        number = format_number(prefix, int(seq), digits)
        taken = (
            await db.execute(
                text("SELECT 1 FROM sales WHERE business_id = :b AND sale_number = :n LIMIT 1"),
                {"b": business_id, "n": number},
            )
        ).first()
        if taken is None:
            return number, int(seq)
    raise HTTPException(status.HTTP_409_CONFLICT, "Could not allocate a unique sale number")
