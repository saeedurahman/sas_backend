"""Structured (JSON) logging with request-ID correlation and secret redaction."""

from __future__ import annotations

import contextvars
import json
import logging
import re
from datetime import datetime, timezone

request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

_REDACTED = "[redacted]"
_SENSITIVE = re.compile(
    r"""(?ix)
    (?P<key>pass(?:word)?|pin(?:_code)?|token|secret|authorization|api[_-]?key|jwt)
    (?P<sep>["']?\s*[:=]\s*["']?)
    (?:bearer\s+)?(?P<value>[^\s,"'}&]+)
    """
)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")


def redact(text: str) -> str:
    text = _BEARER.sub(f"Bearer {_REDACTED}", text)
    return _SENSITIVE.sub(lambda m: f"{m['key']}{m['sep']}{_REDACTED}", text)


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


class JsonFormatter(logging.Formatter):
    _RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
        "message",
        "asctime",
        "request_id",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
            "request_id": getattr(record, "request_id", None),
        }
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            payload[key] = (
                value if isinstance(value, (int, float, bool)) else redact(str(value))
            )
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Uvicorn's own access log would duplicate ours (and logs full URLs).
    logging.getLogger("uvicorn.access").disabled = True
