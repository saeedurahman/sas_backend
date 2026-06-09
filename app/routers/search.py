from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.search import GlobalSearchResponse
from app.services.search_service import global_search

router = APIRouter(prefix="/search", tags=["Search"])


@router.get("", response_model=GlobalSearchResponse, status_code=status.HTTP_200_OK)
async def search(
    q: str = Query(..., min_length=2),
    limit: int = Query(default=20, le=50),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await global_search(db, current_user.business_id, q, limit)
