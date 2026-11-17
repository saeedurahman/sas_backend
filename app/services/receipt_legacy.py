"""Legacy receipt options -> layout config (used by migration 028, testable without alembic).

Before receipt layouts, a business kept receipt options in ``app_settings``
(``receipt.footer_text``, ``receipt.paper_size``, ``receipt.show_tax``, ``receipt.show_logo``) and
sometimes in ``business_configs.config_json`` (``receipt_footer`` or a ``receipt`` object). Those keys are
deprecated: the app can still read them, but rendering uses the layout only. ``apply_legacy`` copies a
business's CHANGED values into a seeded layout once; defaults are never copied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.services.receipt_layout_config import (
    DESIGN_NAMES,
    _edited_urdu as edited_urdu,
    normalise_config,
    upgrade_config,
)

_URDU = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
DEFAULT_FOOTERS = {"", "thank you for shopping with us!", "thank you for shopping with us"}
_PAPER_ALIASES = {
    "58": "roll58", "58mm": "roll58", "roll58": "roll58",
    "80": "roll80", "80mm": "roll80", "roll80": "roll80",
    "a4": "a4",
}
LEGACY_KEYS = ("receipt.footer_text", "receipt.show_tax", "receipt.show_logo", "receipt.paper_size")
SEEDED_NAMES = frozenset(DESIGN_NAMES.values())


@dataclass
class LegacyOptions:
    footer_en: str = ""
    footer_ur: str = ""
    paper: str | None = None  # roll58 | a4 (roll80 is the default and is never copied)
    show_tax: bool | None = None  # only False is a change
    show_logo: bool | None = None  # only False is a change

    @property
    def empty(self) -> bool:
        return not (
            self.footer_en or self.footer_ur or self.paper or self.show_tax is False or self.show_logo is False
        )


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("value", "text"):
            if key in value:
                return value[key]
    return value


def _as_bool(value: Any) -> bool | None:
    value = _unwrap(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
    if isinstance(value, int):
        return bool(value)
    return None


def _footer_parts(value: Any) -> tuple[str, str]:
    """(english, urdu) text of a legacy footer value; the script decides the language."""
    raw = value
    ur_explicit = ""
    if isinstance(raw, dict):
        ur_explicit = str(raw.get("ur") or raw.get("value_ur") or raw.get("text_ur") or "").strip()
    text = _unwrap(raw)
    text = text.strip() if isinstance(text, str) else ""
    en = ur = ""
    if text and text.lower() not in DEFAULT_FOOTERS:
        if _URDU.search(text):
            ur = text
        else:
            en = text
    if ur_explicit:
        ur = ur_explicit
    return en[:300], ur[:300]


def read_legacy(settings: dict[str, Any], config_json: dict[str, Any] | None) -> LegacyOptions:
    """``settings`` maps legacy ``receipt.*`` keys to their stored ``setting_value``; app_settings win."""
    cfg = config_json if isinstance(config_json, dict) else {}
    nested = cfg.get("receipt") if isinstance(cfg.get("receipt"), dict) else {}

    footer_src: Any = settings.get("receipt.footer_text")
    if footer_src is None:
        footer_src = nested.get("footer_text", nested.get("footer", cfg.get("receipt_footer")))
        ur_cfg = nested.get("footer_ur") or cfg.get("receipt_footer_ur")
        if ur_cfg:
            footer_src = {"value": footer_src, "ur": ur_cfg}
    en, ur = _footer_parts(footer_src)

    paper_src = _unwrap(settings.get("receipt.paper_size", nested.get("paper_size")))
    paper = _PAPER_ALIASES.get(str(paper_src).strip().lower()) if paper_src is not None else None
    if paper == "roll80":
        paper = None

    tax = _as_bool(settings["receipt.show_tax"]) if "receipt.show_tax" in settings else _as_bool(nested.get("show_tax"))
    logo = _as_bool(settings["receipt.show_logo"]) if "receipt.show_logo" in settings else _as_bool(nested.get("show_logo"))
    return LegacyOptions(footer_en=en, footer_ur=ur, paper=paper, show_tax=tax, show_logo=logo)


def apply_legacy(
    raw: dict[str, Any] | None, design: str, paper: str, legacy: LegacyOptions
) -> tuple[dict[str, Any], str]:
    """(new v2 config, new paper) for a seeded layout. Language: Urdu edits -> ``both``, else ``en``."""
    both = edited_urdu(raw) or bool(legacy.footer_ur)
    cfg = upgrade_config(raw)
    cfg.setdefault("style", {})["language"] = "both" if both else "en"
    if legacy.footer_en:
        cfg.setdefault("footer", {})["text_en"] = legacy.footer_en
    if legacy.footer_ur:
        cfg.setdefault("footer", {})["text_ur"] = legacy.footer_ur
    if legacy.show_tax is False:
        cfg.setdefault("totals", {})["show_tax_breakdown"] = False
    if legacy.show_logo is False:
        cfg.setdefault("header", {})["show_logo"] = False
    if legacy.paper == "roll58" and design.startswith("thermal_"):
        paper = "roll58"
    return normalise_config(cfg), paper


def run_migration(conn) -> dict[str, int]:
    """Upgrade every stored v<2 layout config; copy legacy options into seeded layouts. Idempotent.

    ``conn`` is a SQLAlchemy sync connection. Only layouts whose config is not yet version 2 are
    touched, so a second run changes nothing.
    """
    import json

    stats = {"upgraded": 0, "copied": 0}
    layouts = conn.exec_driver_sql(
        "SELECT id, business_id, name, design, paper, config FROM receipt_layouts "
        "WHERE deleted_at IS NULL AND COALESCE((config->>'v')::int, 1) < 2 ORDER BY business_id, created_at"
    ).fetchall()
    cache: dict[Any, LegacyOptions] = {}
    for layout_id, business_id, name, design, paper, config in layouts:
        if isinstance(config, str):
            config = json.loads(config)
        legacy = LegacyOptions()
        if name in SEEDED_NAMES:
            if business_id not in cache:
                rows = conn.exec_driver_sql(
                    "SELECT setting_key, setting_value FROM app_settings "
                    "WHERE business_id = %s AND branch_id IS NULL AND deleted_at IS NULL "
                    "AND setting_key = ANY(%s)",
                    (business_id, list(LEGACY_KEYS)),
                ).fetchall()
                cfg_json = conn.exec_driver_sql(
                    "SELECT config_json FROM business_configs WHERE business_id = %s", (business_id,)
                ).scalar()
                if isinstance(cfg_json, str):
                    cfg_json = json.loads(cfg_json)
                cache[business_id] = read_legacy({k: v for k, v in rows}, cfg_json)
            legacy = cache[business_id]
        new_cfg, new_paper = apply_legacy(config, design, paper, legacy)
        conn.exec_driver_sql(
            "UPDATE receipt_layouts SET config = %s::jsonb, paper = %s, updated_at = now() WHERE id = %s",
            (json.dumps(new_cfg), new_paper, layout_id),
        )
        stats["upgraded"] += 1
        if name in SEEDED_NAMES and not legacy.empty:
            stats["copied"] += 1

    # Legacy "a4": the business wanted A4 prints, so its print default becomes the A4 tax invoice
    # (only while it still points at the seeded thermal_classic layout).
    for business_id, legacy in cache.items():
        if legacy.paper == "a4":
            conn.exec_driver_sql(
                """
                UPDATE receipt_layout_assignments a SET layout_id = t.id, updated_at = now()
                  FROM receipt_layouts t, receipt_layouts c
                 WHERE a.business_id = %s AND a.branch_id IS NULL AND a.channel = 'print'
                   AND a.deleted_at IS NULL AND c.id = a.layout_id AND c.design = 'thermal_classic'
                   AND t.business_id = a.business_id AND t.design = 'a4_tax_invoice'
                   AND t.deleted_at IS NULL AND t.is_active
                """,
                (business_id,),
            )
    return stats
