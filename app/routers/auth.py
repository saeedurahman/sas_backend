from fastapi import APIRouter, Depends, Request, status

from app.database import get_db
from app.dependencies import get_current_user, require_permission
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    MessageResponse,
    PinLoginRequest,
    RefreshTokenRequest,
    RegisterBusinessRequest,
    TokenResponse,
    UserInfo,
)
from app.services.login_service import (
    build_user_info,
    login_password_and_build_response,
    login_pin_and_build_response,
    logout_with_refresh_token,
    refresh_access_token,
    register_business_and_authenticate,
)
from app.routers._request_meta import client_ip, user_agent

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    data: RegisterBusinessRequest,
    request: Request,
    db=Depends(get_db),
):
    return await register_business_and_authenticate(
        db, data, client_ip(request), user_agent(request)
    )


@router.post("/login", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def login(
    data: LoginRequest,
    request: Request,
    db=Depends(get_db),
):
    return await login_password_and_build_response(
        db, data.phone, data.password, client_ip(request), user_agent(request)
    )


@router.post("/login/pin", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def login_pin(
    data: PinLoginRequest,
    request: Request,
    db=Depends(get_db),
):
    return await login_pin_and_build_response(
        db,
        data.business_slug,
        data.user_id,
        data.pin_code,
        client_ip(request),
        user_agent(request),
    )


@router.post("/refresh", status_code=status.HTTP_200_OK)
async def refresh(
    data: RefreshTokenRequest,
    db=Depends(get_db),
):
    access_token = await refresh_access_token(db, data.refresh_token)
    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/logout", response_model=MessageResponse, status_code=status.HTTP_200_OK)
async def logout(
    data: RefreshTokenRequest,
    request: Request,
    current_user: User = Depends(require_permission("auth.logout")),
    db=Depends(get_db),
):
    await logout_with_refresh_token(
        db,
        current_user,
        data.refresh_token,
        client_ip(request),
        user_agent(request),
    )
    return MessageResponse(message="Logged out successfully")


@router.get("/me", response_model=UserInfo, status_code=status.HTTP_200_OK)
async def me(current_user: User = Depends(get_current_user)):
    return build_user_info(current_user)
