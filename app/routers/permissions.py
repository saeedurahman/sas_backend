from fastapi import APIRouter, Depends, status

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.role import PermissionsCatalogResponse
from app.services.role_service import get_permissions_catalog

router = APIRouter(prefix="/permissions", tags=["Permissions"])


@router.get(
    "",
    response_model=PermissionsCatalogResponse,
    status_code=status.HTTP_200_OK,
)
async def list_permissions_catalog(
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_permissions_catalog(db)
