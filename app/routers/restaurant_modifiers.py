from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.database import get_db
from app.dependencies import require_feature_flag, require_permission
from app.models.business import BusinessConfig
from app.models.user import User
from app.schemas.auth import MessageResponse
from app.schemas.restaurant import (
    CreateModifierGroupRequest,
    CreateModifierRequest,
    ModifierGroupResponse,
    ModifierResponse,
    ReplaceProductModifierGroupsRequest,
    UpdateModifierGroupRequest,
    UpdateModifierRequest,
    ValidateLineModifiersRequest,
    ValidatedModifierSnapshot,
)
from app.services.restaurant_modifier_service import (
    create_modifier,
    create_modifier_group,
    delete_modifier,
    delete_modifier_group,
    get_modifier_group_by_id,
    get_modifier_groups,
    get_product_modifier_groups,
    replace_product_modifier_groups,
    update_modifier,
    update_modifier_group,
    validate_line_modifiers,
)

router = APIRouter(prefix="/restaurant", tags=["Restaurant"])


@router.get(
    "/modifier-groups",
    response_model=list[ModifierGroupResponse],
    status_code=status.HTTP_200_OK,
)
async def list_modifier_groups(
    current_user: User = Depends(require_permission("restaurant.modifiers.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_restaurant")),
    db=Depends(get_db),
):
    return await get_modifier_groups(db, current_user.business_id)


@router.post(
    "/modifier-groups",
    response_model=ModifierGroupResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_modifier_group_endpoint(
    data: CreateModifierGroupRequest,
    current_user: User = Depends(require_permission("restaurant.modifiers.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_restaurant")),
    db=Depends(get_db),
):
    return await create_modifier_group(
        db,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.get(
    "/modifier-groups/{group_id}",
    response_model=ModifierGroupResponse,
    status_code=status.HTTP_200_OK,
)
async def get_modifier_group(
    group_id: UUID,
    current_user: User = Depends(require_permission("restaurant.modifiers.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_restaurant")),
    db=Depends(get_db),
):
    return await get_modifier_group_by_id(
        db,
        group_id,
        current_user.business_id,
    )


@router.put(
    "/modifier-groups/{group_id}",
    response_model=ModifierGroupResponse,
    status_code=status.HTTP_200_OK,
)
async def update_modifier_group_endpoint(
    group_id: UUID,
    data: UpdateModifierGroupRequest,
    current_user: User = Depends(require_permission("restaurant.modifiers.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_restaurant")),
    db=Depends(get_db),
):
    return await update_modifier_group(
        db,
        group_id,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.delete(
    "/modifier-groups/{group_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_modifier_group_endpoint(
    group_id: UUID,
    current_user: User = Depends(require_permission("restaurant.modifiers.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_restaurant")),
    db=Depends(get_db),
):
    await delete_modifier_group(
        db,
        group_id,
        current_user.business_id,
        current_user.id,
    )
    return MessageResponse(message="Modifier group deleted")


@router.post(
    "/modifier-groups/{group_id}/modifiers",
    response_model=ModifierResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_modifier_endpoint(
    group_id: UUID,
    data: CreateModifierRequest,
    current_user: User = Depends(require_permission("restaurant.modifiers.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_restaurant")),
    db=Depends(get_db),
):
    return await create_modifier(
        db,
        group_id,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.put(
    "/modifiers/{modifier_id}",
    response_model=ModifierResponse,
    status_code=status.HTTP_200_OK,
)
async def update_modifier_endpoint(
    modifier_id: UUID,
    data: UpdateModifierRequest,
    current_user: User = Depends(require_permission("restaurant.modifiers.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_restaurant")),
    db=Depends(get_db),
):
    return await update_modifier(
        db,
        modifier_id,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.delete(
    "/modifiers/{modifier_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_modifier_endpoint(
    modifier_id: UUID,
    current_user: User = Depends(require_permission("restaurant.modifiers.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_restaurant")),
    db=Depends(get_db),
):
    await delete_modifier(
        db,
        modifier_id,
        current_user.business_id,
        current_user.id,
    )
    return MessageResponse(message="Modifier deleted")


@router.get(
    "/products/{product_id}/modifier-groups",
    response_model=list[ModifierGroupResponse],
    status_code=status.HTTP_200_OK,
)
async def get_product_modifier_groups_endpoint(
    product_id: UUID,
    current_user: User = Depends(require_permission("restaurant.modifiers.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_restaurant")),
    db=Depends(get_db),
):
    return await get_product_modifier_groups(
        db,
        product_id,
        current_user.business_id,
    )


@router.put(
    "/products/{product_id}/modifier-groups",
    response_model=list[ModifierGroupResponse],
    status_code=status.HTTP_200_OK,
)
async def replace_product_modifier_groups_endpoint(
    product_id: UUID,
    data: ReplaceProductModifierGroupsRequest,
    current_user: User = Depends(require_permission("restaurant.modifiers.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_restaurant")),
    db=Depends(get_db),
):
    return await replace_product_modifier_groups(
        db,
        product_id,
        current_user.business_id,
        data.modifier_group_ids,
        current_user.id,
    )


@router.post(
    "/modifiers/validate",
    response_model=list[ValidatedModifierSnapshot],
    status_code=status.HTTP_200_OK,
)
async def validate_line_modifiers_endpoint(
    data: ValidateLineModifiersRequest,
    current_user: User = Depends(require_permission("restaurant.modifiers.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_restaurant")),
    db=Depends(get_db),
):
    snapshots = await validate_line_modifiers(
        db,
        current_user.business_id,
        data.product_id,
        data.modifier_ids,
    )
    return [ValidatedModifierSnapshot.model_validate(item) for item in snapshots]
