"""Unit tests for require_feature_flag dependency."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.dependencies import require_feature_flag
from app.models.business import BusinessConfig


def test_require_feature_flag_returns_403_when_disabled() -> None:
    async def _run() -> None:
        config = BusinessConfig(config_json={"enable_kot": False})
        checker = require_feature_flag("enable_kot")

        with pytest.raises(HTTPException) as exc_info:
            await checker(config=config)

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "Feature not enabled: enable_kot"

    asyncio.run(_run())


def test_require_feature_flag_passes_through_when_enabled() -> None:
    async def _run() -> None:
        config = BusinessConfig(config_json={"enable_kot": True})
        checker = require_feature_flag("enable_kot")

        result = await checker(config=config)

        assert result is config

    asyncio.run(_run())


def test_require_feature_flag_defaults_to_false_when_missing() -> None:
    async def _run() -> None:
        config = BusinessConfig(config_json={})
        checker = require_feature_flag("enable_restaurant")

        with pytest.raises(HTTPException) as exc_info:
            await checker(config=config)

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "Feature not enabled: enable_restaurant"

    asyncio.run(_run())
