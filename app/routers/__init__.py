from app.routers.auth import router as auth_router
from app.routers.business import router as business_router
from app.routers.branches import router as branches_router
from app.routers.users import router as users_router

__all__ = [
    "auth_router",
    "business_router",
    "branches_router",
    "users_router",
]
