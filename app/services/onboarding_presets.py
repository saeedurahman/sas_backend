"""
Onboarding-only presets keyed by business_types.code.

Used ONCE when a business registers to build initial business_configs.
Never import this module from routers, services, or jobs after registration.
"""

from typing import Any

# Keys match config_json feature flags (and mirror business_configs boolean columns at insert).
DEFAULT_FLAGS: dict[str, bool] = {
    "enable_restaurant": False,
    "enable_manufacturing": False,
    "enable_loyalty": False,
    "enable_multi_price_list": False,
    "enable_batch_tracking": False,
    "enable_expiry_tracking": False,
    "enable_weight_billing": False,
    "enable_table_management": False,
    "enable_kot": False,
    "enable_offline_mode": True,
    "enable_accounting": False,
    "default_tax_inclusive": False,
    "allow_negative_stock": False,
    "fifo_costing_enabled": True,
}

# Classification code → initial feature flags (registration only).
ONBOARDING_PRESETS_BY_CODE: dict[str, dict[str, bool]] = {
    "bakery": {
        "enable_manufacturing": True,
        "enable_weight_billing": True,
        "enable_expiry_tracking": True,
        "enable_batch_tracking": True,
    },
    "restaurant": {
        "enable_restaurant": True,
        "enable_kot": True,
        "enable_table_management": True,
        "enable_multi_price_list": True,
    },
    "mart": {
        "enable_expiry_tracking": True,
        "enable_batch_tracking": True,
        "enable_weight_billing": True,
        "enable_multi_price_list": True,
    },
    "retail": {
        "enable_loyalty": True,
        "enable_multi_price_list": True,
    },
    "hardware": {
        "enable_multi_price_list": True,
    },
    "pharmacy": {
        "enable_expiry_tracking": True,
        "enable_batch_tracking": True,
    },
    "wholesale": {
        "enable_multi_price_list": True,
        "enable_weight_billing": True,
        "allow_negative_stock": True,
    },
    "electronics": {
        "enable_multi_price_list": True,
        "enable_loyalty": True,
    },
    "salon": {
        "enable_loyalty": True,
    },
    "other": {},
}


def build_onboarding_config_json(business_type_code: str) -> dict[str, Any]:
    """
    Build initial config_json for a new tenant from classification code.
    Called only during registration — not for runtime feature gating.
    """
    flags = dict(DEFAULT_FLAGS)
    overrides = ONBOARDING_PRESETS_BY_CODE.get(business_type_code, {})
    flags.update(overrides)
    return flags


def build_onboarding_config_row(business_type_code: str) -> dict[str, Any]:
    """
    Dict suitable for inserting/updating business_configs boolean columns
    plus config_json at registration time.
    """
    config_json = build_onboarding_config_json(business_type_code)
    return {
        **{k: config_json[k] for k in DEFAULT_FLAGS},
        "config_json": config_json,
        "receipt_prefix": "RCP",
        "invoice_prefix": "INV",
        "po_prefix": "PO",
    }
