from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.branch_scope import assert_branch_access, scoped_branch_query
from app.database import get_db
from app.dependencies import (
    _user_permission_keys,
    allowed_branch_ids,
    get_current_user,
    require_permission,
)
from app.models.user import User
from app.schemas.auth import MessageResponse
from app.schemas.settings import (
    BulkUpsertSettingsRequest,
    SettingResponse,
    TaxPreviewResponse,
    UpsertSettingRequest,
)
from app.services.pricing_engine import price_cart
from app.services.tax_settings import DEFAULTS, load_tax_settings
from app.services.settings_service import (
    can_read_all_settings,
    is_pos_readable_setting,
    bulk_upsert_settings,
    delete_setting,
    get_setting,
    get_settings,
    upsert_setting,
    with_tax_defaults,
)

router = APIRouter(prefix="/settings", tags=["Settings"])


@router.get("", response_model=list[SettingResponse], status_code=status.HTTP_200_OK)
async def list_settings(
    branch_id: UUID | None = Depends(scoped_branch_query),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    settings_rows = await get_settings(db, current_user.business_id, branch_id)
    items = with_tax_defaults(settings_rows, current_user.business_id)
    if can_read_all_settings(_user_permission_keys(current_user)):
        return items
    return [row for row in items if is_pos_readable_setting(row["setting_key"])]


@router.put("", response_model=SettingResponse, status_code=status.HTTP_200_OK)
async def upsert_setting_endpoint(
    data: UpsertSettingRequest,
    current_user: User = Depends(require_permission("settings.manage")),
    db=Depends(get_db),
):
    allowed = allowed_branch_ids(current_user)
    if allowed is not None:
        if data.branch_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Branch-assigned staff cannot modify business-wide settings",
            )
        assert_branch_access(current_user, data.branch_id)
    return await upsert_setting(
        db, current_user.business_id, data, current_user.id
    )


@router.put("/bulk", response_model=list[SettingResponse], status_code=status.HTTP_200_OK)
async def bulk_upsert_settings_endpoint(
    data: BulkUpsertSettingsRequest,
    current_user: User = Depends(require_permission("settings.manage")),
    db=Depends(get_db),
):
    allowed = allowed_branch_ids(current_user)
    if allowed is not None:
        for item in data.settings:
            if item.branch_id is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Branch-assigned staff cannot modify business-wide settings",
                )
            assert_branch_access(current_user, item.branch_id)
    return await bulk_upsert_settings(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/tax-preview",
    response_model=TaxPreviewResponse,
    status_code=status.HTTP_200_OK,
)
async def tax_preview(
    price: Decimal = Query(ge=0, le=Decimal("999999999")),
    qty: Decimal = Query(default=Decimal("1"), gt=0, le=Decimal("999999")),
    rate: Decimal = Query(default=Decimal("0"), ge=0, le=100),
    discount: Decimal = Query(default=Decimal("0"), ge=0),
    current_user: User = Depends(require_permission("sales.create")),
    db=Depends(get_db),
):
    """Break one line down with the business's current tax settings (nothing is saved)."""
    cfg = await load_tax_settings(db, current_user.business_id)
    effective_rate = rate if cfg.enabled else Decimal("0")
    lines, totals = price_cart(
        [
            {
                "qty": qty,
                "unit_price": price,
                "discount_amount": discount,
                "tax_rate": effective_rate,
            }
        ],
        inclusive=cfg.effective_inclusive,
        rounding_mode=cfg.rounding_mode,
    )
    line = lines[0]
    return TaxPreviewResponse(
        tax_enabled=cfg.enabled,
        tax_inclusive=cfg.effective_inclusive,
        tax_rounding_mode=cfg.rounding_mode,
        tax_label=cfg.label,
        tax_rate=effective_rate,
        qty=qty,
        unit_price=price,
        subtotal=line["line_subtotal"],
        discount_amount=line["effective_discount"],
        net_amount=totals["net_amount"],
        tax_amount=totals["total_tax"],
        exact_total=totals["exact_total"],
        rounding_amount=totals["rounding_amount"],
        total_amount=totals["total_amount"],
    )


@router.get(
    "/{setting_key:path}",
    response_model=SettingResponse,
    status_code=status.HTTP_200_OK,
)
async def get_setting_endpoint(
    setting_key: str,
    branch_id: UUID | None = Depends(scoped_branch_query),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    if not can_read_all_settings(
        _user_permission_keys(current_user)
    ) and not is_pos_readable_setting(setting_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission required: settings.view",
        )
    setting = await get_setting(
        db, current_user.business_id, setting_key, branch_id
    )
    if setting is None and setting_key in DEFAULTS:
        return with_tax_defaults([], current_user.business_id)[
            sorted(DEFAULTS).index(setting_key)
        ]
    if setting is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Setting not found",
        )
    return setting


@router.delete(
    "/{setting_key:path}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_setting_endpoint(
    setting_key: str,
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(require_permission("settings.manage")),
    db=Depends(get_db),
):
    allowed = allowed_branch_ids(current_user)
    if allowed is not None:
        if branch_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Branch-assigned staff cannot modify business-wide settings",
            )
        assert_branch_access(current_user, branch_id)
    await delete_setting(
        db,
        current_user.business_id,
        setting_key,
        branch_id,
        current_user.id,
    )
    return MessageResponse(message="Setting deleted")

