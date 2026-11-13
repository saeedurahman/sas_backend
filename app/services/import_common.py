"""Shared plumbing for the CSV import endpoints (products, price lists).

Both imports take either ``csv`` (the file's text) or ``rows`` (already parsed by the app),
normalise headers through an alias table, and answer with the same report::

    {"dry_run": bool,
     "summary": {"total_rows", "create", "update", "unchanged", "errors", "applied"},
     "unknown_columns": [...],
     "rows": [{"row": 2, "action": "create|update|unchanged|error", ...identity, "errors": [], "warnings": []}]}

``row`` is the line number in the file (header = 1) so users can find it in a spreadsheet.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel, Field, model_validator

from app.core.config import settings


class ImportRequest(BaseModel):
    """Body of the import endpoints. Send ``csv`` or ``rows``; ``dry_run`` defaults to true."""

    csv: str | None = None
    rows: list[dict[str, Any]] | None = None
    dry_run: bool = True
    # "abort": one bad row rejects the whole apply (422 + report). "skip": apply valid rows only.
    on_error: str = Field(default="abort", pattern="^(abort|skip)$")

    @model_validator(mode="after")
    def _one_source(self) -> "ImportRequest":
        if (self.csv is None) == (self.rows is None):
            raise ValueError("send exactly one of 'csv' or 'rows'")
        return self


@dataclass
class ParsedRow:
    number: int  # line number in the source file (header = 1)
    values: dict[str, str]


@dataclass
class RowResult:
    row: int
    action: str = "unchanged"  # create | update | unchanged | error
    identity: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def error(self, message: str) -> None:
        self.errors.append(message)
        self.action = "error"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"row": self.row, "action": self.action, **self.identity}
        out.update(self.details)
        out["errors"] = self.errors
        out["warnings"] = self.warnings
        return out


def _norm_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def parse_table(
    body_csv: str | None,
    body_rows: list[dict[str, Any]] | None,
    aliases: dict[str, tuple[str, ...]],
) -> tuple[list[ParsedRow], list[str]]:
    """Return (rows, unknown_columns); headers are mapped to canonical names via ``aliases``."""
    lookup = {_norm_header(alt): canon for canon, alts in aliases.items() for alt in (canon, *alts)}
    unknown: list[str] = []

    def map_header(raw: str) -> str | None:
        canon = lookup.get(_norm_header(raw))
        if canon is None and raw and raw not in unknown:
            unknown.append(raw)
        return canon

    raw_rows: list[tuple[int, dict[str, Any]]] = []
    if body_csv is not None:
        text = body_csv.lstrip("﻿")
        if not text.strip():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The CSV is empty")
        first_line = text.splitlines()[0]
        delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        for index, record in enumerate(reader, start=2):
            raw_rows.append((index, {k: v for k, v in record.items() if k is not None}))
    else:
        for index, record in enumerate(body_rows or [], start=2):
            raw_rows.append((index, dict(record)))

    if len(raw_rows) > settings.import_max_rows:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Too many rows: {len(raw_rows)} (the limit is {settings.import_max_rows}). "
            "Split the file and import it in parts.",
        )

    rows: list[ParsedRow] = []
    for number, record in raw_rows:
        values: dict[str, str] = {}
        for key, value in record.items():
            canon = map_header(str(key))
            if canon is None:
                continue
            values[canon] = "" if value is None else str(value).strip()
        if any(values.values()):
            rows.append(ParsedRow(number, values))
    return rows, unknown


def parse_decimal(raw: str, label: str, *, minimum: Decimal | None = Decimal("0")) -> Decimal:
    """Parse '1,200.50' style numbers; raises ValueError with a user-facing message."""
    text = raw.strip().replace(",", "").replace(" ", "")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{label} '{raw}' is not a number") from None
    if not value.is_finite():
        raise ValueError(f"{label} '{raw}' is not a number")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be {minimum} or more")
    return value


def build_report(
    results: list[RowResult],
    unknown_columns: list[str],
    *,
    dry_run: bool,
    applied: int = 0,
) -> dict[str, Any]:
    counts = {"create": 0, "update": 0, "unchanged": 0, "error": 0}
    for result in results:
        counts[result.action] += 1
    return {
        "dry_run": dry_run,
        "summary": {
            "total_rows": len(results),
            "create": counts["create"],
            "update": counts["update"],
            "unchanged": counts["unchanged"],
            "errors": counts["error"],
            "applied": applied,
        },
        "unknown_columns": unknown_columns,
        "rows": [r.as_dict() for r in results],
    }


def reject_with_report(report: dict[str, Any]) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "code": "import_has_errors",
            "message": (
                f"{report['summary']['errors']} row(s) have errors; nothing was imported. "
                "Fix them, or send on_error=\"skip\" to import only the valid rows."
            ),
            "report": report,
        },
    )
