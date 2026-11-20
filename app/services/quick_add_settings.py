"""Quick Add / pricing-suggestion settings (``app_settings``, business-wide).

Keys (each stored as ``{"value": <v>}`` like every other setting; a missing row = the default):

* ``pricing.default_margin_percent``   number 0-1000, default 15. The client suggests
  ``sale = cost * (1 + margin / 100)`` while scanning; the server never applies it by itself.
* ``pricing.suggested_price_rounding`` ``none | nearest_1 | nearest_5 | nearest_10``, default
  ``nearest_1`` (how the client rounds that suggestion; same vocabulary as ``tax.rounding_mode``).
* ``quick_add.ask_cost``               bool, default true (ask for the cost price per scan).
* ``quick_add.ask_expiry``             bool, default follows the business type: true for pharmacies,
  false otherwise (ask for batch / expiry per scan).
* ``inventory.require_batch``          bool, default false. When true, Quick Add rejects a stock line
  (qty > 0) without ``batch_no`` (``batch_required``).

Changes are audited (``audit_logs``: before value, after value and actor) and travel in the sync
``settings`` entity (``app_settings``), so offline devices behave like the server.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.pricing_engine import ROUNDING_MODES

KEY_MARGIN = "pricing.default_margin_percent"
KEY_ROUNDING = "pricing.suggested_price_rounding"
KEY_ASK_COST = "quick_add.ask_cost"
KEY_ASK_EXPIRY = "quick_add.ask_expiry"
KEY_REQUIRE_BATCH = "inventory.require_batch"

DEFAULTS: dict[str, Any] = {
    KEY_MARGIN: 15,
    KEY_ROUNDING: "nearest_1",
    KEY_ASK_COST: True,
    KEY_ASK_EXPIRY: False,  # the business-type default (pharmacy = True) is applied by effective_defaults()
    KEY_REQUIRE_BATCH: False,
}
SETTING_KEYS = frozenset(DEFAULTS)
AUDITED_KEYS = SETTING_KEYS
MARGIN_MAX = Decimal("1000")
# Business types whose quick-add asks for batch / expiry unless the owner says otherwise.
ASK_EXPIRY_BUSINESS_TYPES = frozenset({"pharmacy"})


def normalize_value(key: str, value: Any) -> Any:
    """Validate + normalise one value. Raises ValueError."""
    if key in (KEY_ASK_COST, KEY_ASK_EXPIRY, KEY_REQUIRE_BATCH):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value
    if key == KEY_ROUNDING:
        if value not in ROUNDING_MODES:
            raise ValueError(f"{key} must be one of: {', '.join(ROUNDING_MODES)}")
        return value
    if key == KEY_MARGIN:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"{key} must be a number from 0 to {MARGIN_MAX}")
        try:
            number = Decimal(str(value).strip())
        except InvalidOperation as exc:
            raise ValueError(f"{key} must be a number from 0 to {MARGIN_MAX}") from exc
        if not number.is_finite() or number < 0 or number > MARGIN_MAX:
            raise ValueError(f"{key} must be a number from 0 to {MARGIN_MAX}")
        number = number.quantize(Decimal("0.01"))
        return int(number) if number == number.to_integral_value() else float(number)
    raise ValueError("unknown setting")


def validate_setting_payload(key: str, setting_value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(setting_value, dict) or "value" not in setting_value:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f'{key} must be sent as {{"value": ...}}'
        )
    try:
        return {"value": normalize_value(key, setting_value["value"])}
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None


def _unwrap(raw: Any) -> Any:
    if isinstance(raw, dict) and "value" in raw:
        return raw["value"]
    return raw


async def business_type_code(db: AsyncSession, business_id: UUID) -> str | None:
    return (
        await db.execute(
            text(
                "SELECT bt.code FROM businesses b JOIN business_types bt ON bt.id = b.business_type_id "
                "WHERE b.id = :b"
            ),
            {"b": business_id},
        )
    ).scalar_one_or_none()


def defaults_for_type(code: str | None) -> dict[str, Any]:
    return {**DEFAULTS, KEY_ASK_EXPIRY: code in ASK_EXPIRY_BUSINESS_TYPES}


async def effective_defaults(db: AsyncSession, business_id: UUID) -> dict[str, Any]:
    """Defaults for this business (``quick_add.ask_expiry`` follows the business type)."""
    return defaults_for_type(await business_type_code(db, business_id))


def effective_value(key: str, stored: dict[str, Any] | None, defaults: dict[str, Any] | None = None) -> Any:
    base = (defaults or DEFAULTS).get(key)
    if stored is None:
        return base
    try:
        return normalize_value(key, _unwrap(stored))
    except ValueError:
        return base


async def load_settings(db: AsyncSession, business_id: UUID) -> dict[str, Any]:
    """Every key with its effective value (stored override, else the business-type default)."""
    defaults = await effective_defaults(db, business_id)
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
    out = dict(defaults)
    for key, raw in rows:
        out[key] = effective_value(key, raw, defaults)
    return out
