"""Optional bot protection for public registration.

Disabled unless ``REGISTRATION_CAPTCHA_ENABLED=true``. The default verifier posts
the client token to a Turnstile / hCaptcha / reCAPTCHA compatible ``siteverify``
endpoint (``CAPTCHA_VERIFY_URL``) with ``CAPTCHA_SECRET``. Swap behaviour by
replacing ``verifier`` (any async callable ``(token, remote_ip) -> bool``).
"""

import logging
from collections.abc import Awaitable, Callable

import httpx
from fastapi import HTTPException, status

from app.core.config import settings

logger = logging.getLogger("app.captcha")

CaptchaVerifier = Callable[[str, str | None], Awaitable[bool]]


async def siteverify(token: str, remote_ip: str | None) -> bool:
    data = {"secret": settings.captcha_secret, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(settings.captcha_verify_url, data=data)
        response.raise_for_status()
        return bool(response.json().get("success"))
    except (httpx.HTTPError, ValueError):
        logger.warning("captcha verification service unavailable")
        return False


verifier: CaptchaVerifier = siteverify


async def verify_registration_captcha(
    token: str | None, remote_ip: str | None
) -> None:
    """No-op when disabled; otherwise raise 400 unless the token verifies."""
    if not settings.registration_captcha_enabled:
        return
    if not token or not await verifier(token, remote_ip):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Captcha verification failed",
        )
