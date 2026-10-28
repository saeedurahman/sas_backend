"""Spawn an isolated uvicorn instance with custom environment variables.

Security tests need settings (tiny rate limits, low lockout thresholds, captcha
on) that differ from a developer's running server, so each group of tests boots
its own app process against the same database.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]

# Generous defaults so unrelated flows (registering tenants, logging in repeatedly)
# are never throttled; individual tests override what they exercise.
RELAXED_ENV = {
    "ENV": "development",
    "RATE_LIMIT_ENABLED": "true",
    "RATE_LIMIT_LOGIN_IP": "100000/minute",
    "RATE_LIMIT_LOGIN_ACCOUNT": "100000/minute",
    "RATE_LIMIT_PIN_IP": "100000/minute",
    "RATE_LIMIT_PIN_USER": "100000/minute",
    "RATE_LIMIT_REFRESH_IP": "100000/minute",
    "RATE_LIMIT_REGISTER_IP": "100000/minute",
    "RATE_LIMIT_PIN_CHANGE_USER": "100000/minute",
    "RATE_LIMIT_PASSWORD_CHANGE_USER": "100000/minute",
    "RATE_LIMIT_CHECK_ALERTS_USER": "100000/minute",
    "MAX_LOGIN_ATTEMPTS": "3",
    "LOCKOUT_MINUTES": "15",
}


# One license key pair for the whole test run; tests verify issued keys with the public half.
from app.licensing.keys import generate_keypair  # noqa: E402

TEST_LICENSE_PRIVATE_KEY, TEST_LICENSE_PUBLIC_KEY = generate_keypair()
TEST_PLATFORM_JWT_SECRET = "test-platform-jwt-secret-0123456789abcdef-xyz"

RELAXED_ENV.update(
    {
        "PLATFORM_JWT_SECRET": TEST_PLATFORM_JWT_SECRET,
        "PLATFORM_ADMIN_RATE_LIMITS": "login_ip=100000/minute,login_account=100000/minute,api=100000/minute",
        "PLATFORM_MAX_LOGIN_ATTEMPTS": "3",
        "PLATFORM_LOCKOUT_MINUTES": "15",
        "RATE_LIMIT_LICENSE_REVOCATIONS": "100000/minute",
        "LICENSE_SIGNING_PRIVATE_KEY": TEST_LICENSE_PRIVATE_KEY,
        "LICENSE_PUBLIC_KEY": TEST_LICENSE_PUBLIC_KEY,
        "TRIAL_DAYS": "14",
        "GRACE_DAYS": "3",
    }
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def managed_server(env: dict[str, str] | None = None):
    """Yield the API base URL (``http://127.0.0.1:<port>/api/v1``) of a fresh server."""
    port = _free_port()
    child_env = {**os.environ, **RELAXED_ENV, **(env or {})}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=child_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    health = f"http://127.0.0.1:{port}/health"
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("test server exited during startup")
            try:
                # Any HTTP answer (even 503 when the DB is deliberately down) = app is up.
                httpx.get(health, timeout=8.0)
                break
            except httpx.HTTPError:
                time.sleep(0.3)
        else:
            raise RuntimeError("test server did not become healthy")
        yield f"http://127.0.0.1:{port}/api/v1"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
