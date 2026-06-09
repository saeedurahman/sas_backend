from fastapi import Request


def client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


def user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")
