"""Fail when a non-public route has no permission / ownership dependency.

    python scripts/route_permission_report.py            # report + exit 1 on violations
    python scripts/route_permission_report.py --list     # print every route and its guard

A route is *guarded* when its dependency tree contains a callable marked with
``__route_guard__`` (``require_permission``, ``require_any_permission``,
``require_owner``, ``require_manager``, ``get_platform_admin``).

Routes that only need a signed-in user and only ever act on that user's own data are
listed in ``SELF_SCOPED`` below, each with the reason. Public (no auth) routes are in
``PUBLIC``. Anything else is a violation - add a guard, or add it to a list with a reason
in code review.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if __name__ == "__main__":
    # CI has no .env; when imported (tests) the real settings must stay untouched,
    # otherwise child servers would inherit the fake DATABASE_URL.
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://ci:ci@localhost:5432/ci")
    os.environ.setdefault("JWT_SECRET_KEY", "ci-only-access-secret-not-for-deploys")
    os.environ.setdefault("JWT_REFRESH_SECRET_KEY", "ci-only-refresh-secret-not-for-deploys")

from fastapi.routing import APIRoute  # noqa: E402

from app.core.config import settings  # noqa: E402

V1 = settings.api_v1_prefix

# (method, path) with no authentication at all.
PUBLIC: dict[tuple[str, str], str] = {
    ("GET", "/health"): "load-balancer health probe",
    ("POST", f"{V1}/auth/register"): "business sign-up (captcha + rate limited)",
    ("POST", f"{V1}/auth/login"): "sign-in (rate limited, lockout)",
    ("POST", f"{V1}/auth/login/pin"): "PIN sign-in (rate limited, lockout)",
    ("POST", f"{V1}/auth/refresh"): "token rotation (refresh token is the credential)",
    ("POST", "/platform/auth/login"): "admin sign-in (password + TOTP, rate limited, lockout)",
    ("GET", "/licenses/revocations"): "signed revocation list for the offline edition (rate limited)",
}

# Signed-in user acting on their own account/session only.
SELF_SCOPED: dict[tuple[str, str], str] = {
    ("GET", f"{V1}/auth/me"): "own profile",
    ("POST", f"{V1}/auth/change-password"): "own password (needs current password)",
    ("GET", f"{V1}/subscription/status"): "own business subscription state",
    ("GET", f"{V1}/shifts/my-active"): "own open shift",
    ("GET", f"{V1}/business/me"): "own business profile",
    ("GET", f"{V1}/business/types"): "reference list of business types (no tenant data)",
    ("GET", f"{V1}/settings"): "handler filters rows by the caller's permissions",
    ("GET", f"{V1}/settings/{{setting_key:path}}"): "handler filters by the caller's permissions",
    ("PUT", f"{V1}/users/{{user_id}}/pin"): "handler: own PIN, or a manager with users.update",
}

# FastAPI's own documentation routes.
FRAMEWORK = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def _guards(dependant) -> list[str]:
    found: list[str] = []
    stack = [dependant]
    while stack:
        node = stack.pop()
        marker = getattr(node.call, "__route_guard__", None)
        if marker:
            found.append(marker)
        stack.extend(node.dependencies)
    return found


def collect(app=None) -> list[tuple[str, str, list[str]]]:
    if app is None:
        import main  # noqa: PLC0415 - imported late so env defaults above apply

        app = main.app
    rows: list[tuple[str, str, list[str]]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.path in FRAMEWORK:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            rows.append((method, route.path, _guards(route.dependant)))
    return sorted(rows, key=lambda r: (r[1], r[0]))


def find_violations(rows: list[tuple[str, str, list[str]]]) -> list[str]:
    """"METHOD path" for every route that is neither public, guarded nor self-scoped."""
    return [
        f"{method} {path}"
        for method, path, guards in rows
        if (method, path) not in PUBLIC and not guards and (method, path) not in SELF_SCOPED
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="print every route")
    args = parser.parse_args()

    rows = collect()
    violations: list[str] = []
    stale: list[str] = []
    seen = {(m, p) for m, p, _ in rows}
    for key in [*PUBLIC, *SELF_SCOPED]:
        if key not in seen:
            stale.append(f"{key[0]} {key[1]}")

    for method, path, guards in rows:
        key = (method, path)
        if key in PUBLIC:
            label = "public"
        elif guards:
            label = ", ".join(sorted(set(guards)))
        elif key in SELF_SCOPED:
            label = "self-scoped"
        else:
            label = "!! UNGUARDED"
            violations.append(f"{method} {path}")
        if args.list or label.startswith("!!"):
            print(f"{method:7} {path:60} {label}")

    guarded = sum(1 for m, p, g in rows if g)
    print(
        f"\n{len(rows)} routes: {guarded} guarded, "
        f"{sum(1 for r in rows if (r[0], r[1]) in PUBLIC)} public, "
        f"{sum(1 for r in rows if (r[0], r[1]) in SELF_SCOPED and not r[2])} self-scoped, "
        f"{len(violations)} unguarded"
    )
    if stale:
        print("Allow-list entries for routes that no longer exist: " + "; ".join(stale))
    if violations:
        print("FAIL: add require_permission(...) / require_owner, or justify in the allow-lists.")
        return 1
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
