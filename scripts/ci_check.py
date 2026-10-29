"""CI gate for the security baseline.

    python scripts/ci_check.py                 # static checks + unit tests (no DB)
    python scripts/ci_check.py --integration   # + security suite (needs TEST_DATABASE_URL, migrated DB)

Exit code is non-zero when any check fails.
"""

from __future__ import annotations

import argparse
import compileall
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Settings need these to import; CI should not need a real .env.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://ci:ci@localhost:5432/ci")
os.environ.setdefault("JWT_SECRET_KEY", "ci-only-access-secret-not-for-deploys")
os.environ.setdefault("JWT_REFRESH_SECRET_KEY", "ci-only-refresh-secret-not-for-deploys")

from tests.helpers.testdb import configured_test_database_url  # noqa: E402

# Unit tests never connect, but pytest insists on a test database distinct from DATABASE_URL.
USING_PLACEHOLDER_TEST_DB = configured_test_database_url() is None
if USING_PLACEHOLDER_TEST_DB:
    os.environ["TEST_DATABASE_URL"] = "postgresql+asyncpg://ci:ci@localhost:5432/ci_test"

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return result.stdout.strip()


def check_env_not_tracked() -> None:
    tracked = [p for p in git("ls-files").splitlines() if re.fullmatch(r"\.env(\..+)?", Path(p).name)]
    leaked = [p for p in tracked if not p.endswith(".env.example")]
    check(".env files are not tracked by git", not leaked, ", ".join(leaked))
    ignored = (ROOT / ".gitignore").read_text(encoding="utf8") if (ROOT / ".gitignore").exists() else ""
    check(".gitignore ignores .env", re.search(r"^\.env\s*$", ignored, re.M) is not None)


def check_env_example() -> None:
    from app.core.config import Settings

    example = (ROOT / ".env.example").read_text(encoding="utf8")
    keys = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", example, re.M))
    aliases = {
        field.validation_alias
        for field in Settings.model_fields.values()
        if isinstance(field.validation_alias, str)
    }
    missing = sorted(aliases - keys)
    check(".env.example documents every setting", not missing, f"missing: {missing}")

    values = dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", example, re.M))
    check(
        ".env.example holds no real secrets",
        values.get("JWT_SECRET_KEY", "") == ""
        and values.get("JWT_REFRESH_SECRET_KEY", "") == ""
        and "<" in values.get("DATABASE_URL", "<")
        and values.get("CAPTCHA_SECRET", "") == "",
    )


def check_compiles() -> None:
    ok = all(
        compileall.compile_dir(ROOT / d, quiet=1, force=True)
        for d in ("app", "tests", "scripts", "alembic")
    ) and compileall.compile_file(ROOT / "main.py", quiet=1, force=True)
    check("python sources compile", bool(ok))


def check_migrations_single_head() -> None:
    versions = ROOT / "alembic" / "versions"
    revisions = {}
    for path in versions.glob("*.py"):
        text = path.read_text(encoding="utf8")
        rev = re.search(r'^revision[^=]*=\s*"([^"]+)"', text, re.M)
        down = re.search(r'^down_revision[^=]*=\s*(?:"([^"]+)"|None)', text, re.M)
        if rev:
            revisions[rev.group(1)] = down.group(1) if down else None
    parents = {d for d in revisions.values() if d}
    heads = [r for r in revisions if r not in parents]
    check("alembic has a single head", len(heads) == 1, f"heads: {heads}")


def check_route_guards() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "route_permission_report.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    check("every non-public route has a permission/ownership guard", result.returncode == 0, result.stdout[-600:])


def run_pytest(label: str, *paths: str, extra: tuple[str, ...] = ()) -> None:
    cmd = [sys.executable, "-m", "pytest", *paths, "-q", "-p", "no:cacheprovider", *extra]
    result = subprocess.run(cmd, cwd=ROOT)
    check(label, result.returncode == 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--integration", action="store_true", help="also run DB-backed security tests")
    args = parser.parse_args()

    check_env_not_tracked()
    check_env_example()
    check_compiles()
    check_migrations_single_head()
    check_route_guards()
    run_pytest(
        "unit tests",
        "tests/unit/test_security_config.py",
        "tests/unit/test_rate_limit.py",
        "tests/unit/test_http_hardening.py",
        "tests/unit/test_require_feature_flag.py",
        "tests/unit/test_license_keys.py",
        "tests/unit/test_subscription_state.py",
    )
    if args.integration:
        if USING_PLACEHOLDER_TEST_DB:
            check("TEST_DATABASE_URL is set for integration tests", False, "set it, then run scripts/create_test_db.py")
            print()
            print("FAILED: " + "; ".join(FAILURES))
            return 1
        run_pytest("security integration tests", "tests/security")

    print()
    if FAILURES:
        print("FAILED: " + "; ".join(FAILURES))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
