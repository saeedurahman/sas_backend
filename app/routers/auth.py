from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from app.core.config import settings
from app.core.rate_limit import enforce_rate_limit, ip_rate_limit
from app.database import get_db
from app.dependencies import (
    get_current_user,
    require_permission,
    subscription_exempt,
)
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    MeResponse,
    MessageResponse,
    PinLoginRequest,
    RefreshTokenRequest,
    RefreshTokenResponse,
    RegisterBusinessRequest,
    TokenResponse,
    UserInfo,
)
from app.services.login_service import (
    build_subscription_summary,
    build_user_info,
    login_password_and_build_response,
    login_pin_and_build_response,
    logout_with_refresh_token,
    refresh_tokens,
    register_business_and_authenticate,
    lease_for_business,
    subscription_summary_for_business,
)
from app.routers._request_meta import client_ip, user_agent
from app.services.captcha_service import verify_registration_captcha
from app.services.lease_service import issue_lease
from app.services.user_service import change_own_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(ip_rate_limit("register-ip", "rate_limit_register_ip"))],
)
async def register(
    data: RegisterBusinessRequest,
    request: Request,
    db=Depends(get_db),
):
    await verify_registration_captcha(data.captcha_token, client_ip(request))
    return await register_business_and_authenticate(
        db, data, client_ip(request), user_agent(request)
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(ip_rate_limit("login-ip", "rate_limit_login_ip"))],
)
async def login(
    data: LoginRequest,
    request: Request,
    db=Depends(get_db),
):
    enforce_rate_limit(
        "login-account",
        f"{data.business_slug or '-'}:{data.phone}",
        settings.rate_limit_login_account,
    )
    return await login_password_and_build_response(
        db,
        data.phone,
        data.password,
        client_ip(request),
        user_agent(request),
        data.business_slug,
    )


@router.post(
    "/login/pin",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(ip_rate_limit("pin-ip", "rate_limit_pin_ip"))],
)
async def login_pin(
    data: PinLoginRequest,
    request: Request,
    db=Depends(get_db),
):
    enforce_rate_limit("pin-user", str(data.user_id), settings.rate_limit_pin_user)
    return await login_pin_and_build_response(
        db,
        data.business_slug,
        data.user_id,
        data.pin_code,
        client_ip(request),
        user_agent(request),
    )


@router.post(
    "/refresh",
    response_model=RefreshTokenResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(ip_rate_limit("refresh-ip", "rate_limit_refresh_ip"))],
)
async def refresh(
    data: RefreshTokenRequest,
    request: Request,
    db=Depends(get_db),
):
    """Rotates the refresh token: store the returned one; the one sent is now dead."""
    access_token, new_refresh_token = await refresh_tokens(
        db, data.refresh_token, client_ip(request), user_agent(request)
    )
    # Re-check the business on every refresh and report its current subscription.
    from app.services.auth_service import decode_token

    business_id = UUID(str(decode_token(access_token)["business_id"]))
    return RefreshTokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        subscription=await subscription_summary_for_business(db, business_id),
        lease=await lease_for_business(db, business_id),
    )


@router.post(
    "/change-password",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def change_password(
    data: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Requires the current password. Signs the user out of every session."""
    enforce_rate_limit(
        "password-change",
        str(current_user.id),
        settings.rate_limit_password_change_user,
    )
    await change_own_password(
        db, current_user, data.current_password, data.new_password
    )
    return MessageResponse(message="Password changed. Please sign in again.")


@router.post(
    "/logout",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(subscription_exempt)],
)
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


@router.get(
    "/me",
    response_model=MeResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(subscription_exempt)],
)
async def me(current_user: User = Depends(get_current_user)):
    info = build_user_info(current_user)
    return MeResponse(
        **info.model_dump(),
        subscription=build_subscription_summary(current_user.business),
        lease=issue_lease(current_user.business),
    )
