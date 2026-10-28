"""Item 1: production configuration guards and docs lockdown (no DB needed)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
GOOD_A = "k9Xw2mQz7Lr4Tn8Vb3Yc6Hd1Fs5Gj0PaUeRiOyWt"
GOOD_B = "Zq3Mn8Bv1Cx6Lk9Jh2Gf5Ds4Aa7Ps0OiUyTrEwQp"


def prod(**overrides):
    values = dict(
        env="production",
        database_url="postgresql://u:p@localhost/db",
        jwt_secret_key=GOOD_A,
        jwt_refresh_secret_key=GOOD_B,
        allowed_origins="https://app.example.com",
        debug=False,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_valid_production_config_passes():
    s = prod()
    assert s.is_production and s.cors_origins == ["https://app.example.com"]


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"jwt_secret_key": "short"}, "JWT_SECRET_KEY must be at least 32"),
        ({"jwt_refresh_secret_key": "x" * 31}, "JWT_REFRESH_SECRET_KEY must be at least 32"),
        ({"jwt_secret_key": "change-me-access-secret-" + "x" * 20}, "placeholder"),
        ({"jwt_refresh_secret_key": GOOD_A}, "must differ"),
        ({"allowed_origins": ""}, "ALLOWED_ORIGINS must be set"),
        ({"allowed_origins": " , "}, "ALLOWED_ORIGINS must be set"),
        ({"allowed_origins": "https://a.com,*"}, "must not contain"),
        ({"debug": True}, "DEBUG must be false"),
        ({"rate_limit_enabled": False}, "RATE_LIMIT_ENABLED"),
        ({"registration_captcha_enabled": True, "captcha_secret": ""}, "CAPTCHA_SECRET"),
    ],
)
def test_unsafe_production_config_is_rejected(overrides, fragment):
    with pytest.raises(ValidationError) as exc:
        prod(**overrides)
    assert fragment in str(exc.value)


def test_development_allows_weak_settings():
    s = Settings(
        _env_file=None,
        env="development",
        database_url="postgresql://u:p@localhost/db",
        jwt_secret_key="dev",
        jwt_refresh_secret_key="dev2",
        debug=True,
    )
    assert not s.is_production


def _run_import(env: dict[str, str]):
    code = (
        "import main\n"
        "from fastapi.testclient import TestClient\n"
        "c = TestClient(main.app)\n"
        "print([main.app.docs_url, main.app.redoc_url, main.app.openapi_url])\n"
        "print([c.get(p).status_code for p in ('/docs', '/redoc', '/openapi.json')])\n"
    )
    base = {k: v for k, v in os.environ.items() if k not in {"ENV", "DEBUG", "ALLOWED_ORIGINS"}}
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_startup_fails_in_production_with_weak_secret():
    proc = _run_import(
        {"ENV": "production", "JWT_SECRET_KEY": "weak", "ALLOWED_ORIGINS": "https://a.com"}
    )
    assert proc.returncode != 0
    assert "Unsafe production configuration" in proc.stderr


def test_startup_fails_in_production_without_origins():
    proc = _run_import(
        {
            "ENV": "production",
            "JWT_SECRET_KEY": GOOD_A,
            "JWT_REFRESH_SECRET_KEY": GOOD_B,
            "ALLOWED_ORIGINS": "",
        }
    )
    assert proc.returncode != 0
    assert "ALLOWED_ORIGINS must be set" in proc.stderr


def test_docs_disabled_in_production_enabled_in_development():
    prod_run = _run_import(
        {
            "ENV": "production",
            "JWT_SECRET_KEY": GOOD_A,
            "JWT_REFRESH_SECRET_KEY": GOOD_B,
            "ALLOWED_ORIGINS": "https://a.com",
        }
    )
    assert prod_run.returncode == 0, prod_run.stderr
    assert "[None, None, None]" in prod_run.stdout
    assert "[404, 404, 404]" in prod_run.stdout

    dev_run = _run_import({"ENV": "development"})
    assert dev_run.returncode == 0, dev_run.stderr
    assert "[200, 200, 200]" in dev_run.stdout
