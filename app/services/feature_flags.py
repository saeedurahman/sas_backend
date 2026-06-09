"""
Runtime feature checks — ALWAYS read from business_configs.config_json.

ARCHITECTURE RULE: Never branch on business_type or business_types.code
after registration. Those are classification / onboarding presets only.
"""

from typing import Any


def get_feature_flag(config_json: dict[str, Any] | None, key: str, default: bool = False) -> bool:
    """Return a feature flag from config_json (sole runtime source of truth)."""
    if not config_json:
        return default
    value = config_json.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def merge_config_json(
    base: dict[str, Any] | None,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(base or {})
    merged.update(overrides)
    return merged
