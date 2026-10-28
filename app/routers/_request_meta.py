from fastapi import Request

from app.core.rate_limit import client_ip as _resolve_client_ip


def client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return _resolve_client_ip(request)


def user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")
