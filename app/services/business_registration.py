"""
Business registration — applies onboarding presets from business_types.code once.

After registration, all feature checks must use feature_flags.get_feature_flag()
against business_configs.config_json only. Never read business_type_id/code for gating.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.services.onboarding_presets import build_onboarding_config_row


def resolve_business_type_id(connection: Connection, business_type_code: str) -> UUID:
    type_id = connection.execute(
        text(
            """
            SELECT id FROM business_types
            WHERE code = :code AND is_active = TRUE
            """
        ),
        {"code": business_type_code},
    ).scalar_one_or_none()
    if type_id is None:
        raise ValueError(f"Unknown or inactive business type code: {business_type_code}")
    return type_id


def create_business_config_for_registration(
    connection: Connection,
    *,
    business_id: UUID,
    business_type_code: str,
    created_by: UUID | None = None,
) -> UUID:
    """
    Insert business_configs using onboarding preset for the classification code.
    """
    preset = build_onboarding_config_row(business_type_code)
    config_json = preset.pop("config_json")

    result = connection.execute(
        text(
            """
            INSERT INTO business_configs (
                business_id,
                enable_restaurant, enable_manufacturing, enable_loyalty,
                enable_multi_price_list, enable_batch_tracking, enable_expiry_tracking,
                enable_weight_billing, enable_table_management, enable_kot,
                enable_offline_mode, enable_accounting, default_tax_inclusive,
                allow_negative_stock, fifo_costing_enabled,
                receipt_prefix, invoice_prefix, po_prefix,
                config_json, created_by
            ) VALUES (
                :business_id,
                :enable_restaurant, :enable_manufacturing, :enable_loyalty,
                :enable_multi_price_list, :enable_batch_tracking, :enable_expiry_tracking,
                :enable_weight_billing, :enable_table_management, :enable_kot,
                :enable_offline_mode, :enable_accounting, :default_tax_inclusive,
                :allow_negative_stock, :fifo_costing_enabled,
                :receipt_prefix, :invoice_prefix, :po_prefix,
                CAST(:config_json AS jsonb), :created_by
            )
            RETURNING id
            """
        ),
        {
            "business_id": business_id,
            "config_json": _json_dumps(config_json),
            "created_by": created_by,
            **preset,
        },
    )
    return result.scalar_one()


def register_business(
    connection: Connection,
    *,
    name: str,
    business_type_code: str,
    email: str | None = None,
    phone: str | None = None,
    created_by: UUID | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Create businesses + business_configs. Uses business_type code only to pick
    initial config_json — never store code on businesses for feature logic.
    """
    business_type_id = resolve_business_type_id(connection, business_type_code)

    biz = connection.execute(
        text(
            """
            INSERT INTO businesses (name, business_type_id, email, phone, created_by)
            VALUES (:name, :business_type_id, :email, :phone, :created_by)
            RETURNING id
            """
        ),
        {
            "name": name,
            "business_type_id": business_type_id,
            "email": email,
            "phone": phone,
            "created_by": created_by,
        },
    )
    business_id = biz.scalar_one()

    config_id = create_business_config_for_registration(
        connection,
        business_id=business_id,
        business_type_code=business_type_code,
        created_by=created_by,
    )

    return {
        "business_id": business_id,
        "business_config_id": config_id,
        "business_type_id": business_type_id,
    }


def _json_dumps(data: dict[str, Any]) -> str:
    import json

    return json.dumps(data)
