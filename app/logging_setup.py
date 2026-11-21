"""Logging with a safety net that redacts anything that looks like a Plaid token or API key."""

from __future__ import annotations

import logging
import re

_PATTERNS = [
    re.compile(r"(access|public|link)-(sandbox|production|development)-[0-9a-fA-F-]{8,}"),
    re.compile(r"(?i)(x-api-key|authorization|plaid-secret|secret|access_token)(['\"]?\s*[:=]\s*['\"]?)([^'\"\s,}]+)"),
]


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover
            return True
        red = msg
        red = _PATTERNS[0].sub("[REDACTED-TOKEN]", red)
        red = _PATTERNS[1].sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", red)
        if red != msg:
            record.msg, record.args = red, ()
        return True


def redact(text: str) -> str:
    text = _PATTERNS[0].sub("[REDACTED-TOKEN]", text)
    return _PATTERNS[1].sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", text)


def setup_logging() -> None:
    root = logging.getLogger()
    if not any(isinstance(f, RedactingFilter) for h in root.handlers for f in h.filters):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        for h in logging.getLogger().handlers:
            h.addFilter(RedactingFilter())
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        for h in logging.getLogger(name).handlers:
            h.addFilter(RedactingFilter())
