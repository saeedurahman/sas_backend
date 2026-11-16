"""Per-business tax and price settings (stored in ``app_settings``, business-wide).

Every value is stored as ``{"value": <v>}`` like the other settings. Missing rows fall
back to ``DEFAULTS`` so businesses that existed before these settings keep their old
behaviour (tax on, exclusive prices, no rounding).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.pricing_engine import ROUNDING_MODES

KEY_ENABLED = "tax.enabled"
KEY_INCLUSIVE = "tax.prices_include_tax"
KEY_ROUNDING = "tax.rounding_mode"
KEY_BREAKDOWN = "tax.show_breakdown_on_receipt"
KEY_LABEL = "tax.label"
KEY_DEFAULT_RATE = "tax.default_rate_id"
KEY_HIDE_ZERO = "ui.hide_zero_decimals"

DEFAULTS: dict[str, Any] = {
    KEY_ENABLED: True,
    KEY_INCLUSIVE: False,
    KEY_ROUNDING: "none",
    KEY_BREAKDOWN: True,
    KEY_LABEL: "GST",
    KEY_DEFAULT_RATE: None,
    KEY_HIDE_ZERO: False,
}

TAX_SETTING_KEYS = frozenset(DEFAULTS)
# Changes to these are audited (before/after/actor) and only affect new sales.
AUDITED_KEYS = frozenset({KEY_INCLUSIVE, KEY_ROUNDING})
LABEL_MAX_LEN = 30


@dataclass(frozen=True)
class TaxSettings:
    enabled: bool = True
    prices_include_tax: bool = False
    rounding_mode: str = "none"
    show_breakdown_on_receipt: bool = True
    label: str = "GST"
    default_rate_id: str | None = None
    hide_zero_decimals: bool = False

    @property
    def effective_inclusive(self) -> bool:
        """Inclusive pricing only means something while tax is on."""
        return self.enabled and self.prices_include_tax

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def as_settings_map(self) -> dict[str, Any]:
        """Wire shape keyed by setting key (sync settings subset)."""
        return {
            KEY_ENABLED: self.enabled,
            KEY_INCLUSIVE: self.prices_include_tax,
            KEY_ROUNDING: self.rounding_mode,
            KEY_BREAKDOWN: self.show_breakdown_on_receipt,
            KEY_LABEL: self.label,
            KEY_DEFAULT_RATE: self.default_rate_id,
            KEY_HIDE_ZERO: self.hide_zero_decimals,
        }


def _unwrap(raw: Any) -> Any:
    if isinstance(raw, dict) and "value" in raw:
        return raw["value"]
    return raw


def settings_from_values(values: dict[str, Any]) -> TaxSettings:
    """Build settings from raw stored values, ignoring anything malformed."""
    merged = dict(DEFAULTS)
    for key, raw in values.items():
        if key not in DEFAULTS:
            continue
        try:
            merged[key] = normalize_value(key, _unwrap(raw))
        except ValueError:
            continue
    return TaxSettings(
        enabled=merged[KEY_ENABLED],
        prices_include_tax=merged[KEY_INCLUSIVE],
        rounding_mode=merged[KEY_ROUNDING],
        show_breakdown_on_receipt=merged[KEY_BREAKDOWN],
        label=merged[KEY_LABEL],
        default_rate_id=merged[KEY_DEFAULT_RATE],
        hide_zero_decimals=merged[KEY_HIDE_ZERO],
    )


async def load_tax_settings(db: AsyncSession, business_id: UUID) -> TaxSettings:
    rows = (
        await db.execute(
            text(
                "SELECT setting_key, setting_value FROM app_settings "
                "WHERE business_id = :b AND branch_id IS NULL AND deleted_at IS NULL "
                "AND (setting_key LIKE 'tax.%' OR setting_key = 'ui.hide_zero_decimals')"
            ),
            {"b": business_id},
        )
    ).all()
    return settings_from_values({r.setting_key: r.setting_value for r in rows})


async def tax_settings_at(
    db: AsyncSession, business_id: UUID, moment: datetime
) -> tuple[TaxSettings, TaxSettings]:
    """(settings active at ``moment``, current settings).

    Only price-mode and rounding changes are audited, so those two keys are rewound to
    their value at ``moment`` using the first audited change after it.
    """
    current = await load_tax_settings(db, business_id)
    values = current.as_settings_map()
    for key in AUDITED_KEYS:
        row = (
            await db.execute(
                text(
                    "SELECT old_values FROM audit_logs "
                    "WHERE business_id = :b AND table_name = 'app_settings' "
                    "AND new_values->>'setting_key' = :k AND created_at > :t "
                    "ORDER BY created_at ASC LIMIT 1"
                ),
                {"b": business_id, "k": key, "t": moment},
            )
        ).first()
        if row is not None and isinstance(row.old_values, dict) and "value" in row.old_values:
            values[key] = row.old_values["value"]
    return settings_from_values(values), current


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def normalize_value(key: str, value: Any) -> Any:
    """Validate + normalise one tax/ui setting value. Raises ValueError."""
    if key in (KEY_ENABLED, KEY_INCLUSIVE, KEY_BREAKDOWN, KEY_HIDE_ZERO):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value
    if key == KEY_ROUNDING:
        if value not in ROUNDING_MODES:
            raise ValueError(f"{key} must be one of: {', '.join(ROUNDING_MODES)}")
        return value
    if key == KEY_LABEL:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
        value = value.strip()
        if len(value) > LABEL_MAX_LEN:
            raise ValueError(f"{key} must be at most {LABEL_MAX_LEN} characters")
        return value
    if key == KEY_DEFAULT_RATE:
        if value is None or value == "":
            return None
        try:
            return str(UUID(str(value)))
        except ValueError as exc:
            raise ValueError(f"{key} must be a tax rate id or null") from exc
    return value


def validate_setting_payload(key: str, setting_value: dict[str, Any]) -> dict[str, Any]:
    """Validate a PUT /settings body for a tax/ui key; other keys pass through untouched."""
    if key not in TAX_SETTING_KEYS:
        return setting_value
    if not isinstance(setting_value, dict) or "value" not in setting_value:
        raise _bad(f'{key} must be sent as {{"value": ...}}')
    try:
        return {"value": normalize_value(key, setting_value["value"])}
    except ValueError as exc:
        raise _bad(str(exc)) from exc


def effective_value(key: str, stored: dict[str, Any] | None) -> Any:
    if stored is None:
        return DEFAULTS.get(key)
    try:
        return normalize_value(key, _unwrap(stored))
    except ValueError:
        return DEFAULTS.get(key)

