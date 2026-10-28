"""The CI route guard report: every non-public route needs a permission/ownership guard."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi import Depends, FastAPI

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import route_permission_report as report  # noqa: E402

from app.dependencies import get_current_user, require_owner, require_permission  # noqa: E402
from app.platform_deps import get_platform_admin  # noqa: E402


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/guarded")
    async def guarded(_=Depends(require_permission("things.view"))):
        return {}

    @app.get("/owner-only")
    async def owner_only(_=Depends(require_owner)):
        return {}

    @app.get("/admin-only")
    async def admin_only(_=Depends(get_platform_admin)):
        return {}

    @app.get("/just-logged-in")
    async def just_logged_in(_=Depends(get_current_user)):
        return {}

    @app.get("/open")
    async def open_route():
        return {}

    @app.get("/via-decorator", dependencies=[Depends(require_permission("x.y"))])
    async def via_decorator():
        return {}

    return app


def test_guards_are_detected_including_nested_and_decorator_dependencies():
    rows = {(m, p): g for m, p, g in report.collect(_app())}
    assert rows[("GET", "/guarded")] == ["permission:things.view"]
    assert rows[("GET", "/owner-only")] == ["owner"]
    assert rows[("GET", "/admin-only")] == ["platform_admin"]
    assert rows[("GET", "/via-decorator")] == ["permission:x.y"]
    assert rows[("GET", "/just-logged-in")] == [] and rows[("GET", "/open")] == []


def test_unguarded_and_merely_logged_in_routes_are_violations():
    violations = report.find_violations(report.collect(_app()))
    assert sorted(violations) == ["GET /just-logged-in", "GET /open"]


def test_real_app_has_no_unguarded_routes():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "route_permission_report.py")],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "0 unguarded" in proc.stdout


def test_real_app_report_lists_every_platform_route_as_guarded():
    rows = {(m, p): g for m, p, g in report.collect()}
    platform = {k: g for k, g in rows.items() if k[1].startswith("/platform/")}
    assert len(platform) > 25
    for (method, path), guards in platform.items():
        if path == "/platform/auth/login":
            assert (method, path) in report.PUBLIC
        else:
            assert guards == ["platform_admin"], (method, path)


def test_public_allow_list_is_exactly_what_is_unauthenticated():
    rows = {(m, p): g for m, p, g in report.collect()}
    for key in report.PUBLIC:
        assert key in rows and rows[key] == []
    unlisted_open = [
        k for k, g in rows.items()
        if not g and k not in report.PUBLIC and k not in report.SELF_SCOPED
    ]
    assert unlisted_open == []


def test_branch_scope_is_wired_into_every_router_named_in_the_spec():
    for name in ("sales", "shifts", "stock", "transfers", "expenses", "purchases", "registers",
                 "returns", "adjustments", "waste"):
        src = (ROOT / "app" / "routers" / f"{name}.py").read_text(encoding="utf8")
        assert "branch_scope" in src, name
