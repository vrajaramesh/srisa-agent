"""JSON logging to stdout in the shape Google Cloud Logging understands.

Cloud Run forwards each stdout line to Cloud Logging; if the line is a JSON object,
the `severity` and `message` fields are mapped to the log entry's level and summary,
and every other field becomes searchable structured data (jsonPayload.*).
"""

import json
import logging
import re
import sys
from datetime import datetime, timezone

# Python level name -> Cloud Logging severity
_SEVERITY = {
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
}


# The GET handshake puts our verify token in the query string, and uvicorn's access
# log prints full URLs. Mask it before anything is written.
_VERIFY_TOKEN_RE = re.compile(r"(hub\.verify_token=)[^&\s\"]*")


class RedactSecretsFilter(logging.Filter):
    """Mask `hub.verify_token=...` in any log record (mainly uvicorn.access)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        return True


def _redact(text: str) -> str:
    """Replace the verify token value with ***."""
    return _VERIFY_TOKEN_RE.sub(r"\1***", text)


class JsonFormatter(logging.Formatter):
    """Render each record as one JSON object per line.

    Extra structured fields are passed as `logger.info("msg", extra={"fields": {...}})`.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "severity": _SEVERITY.get(record.levelname, "DEFAULT"),
            "message": record.getMessage(),
            "logger": record.name,
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            entry.update(fields)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Replace root handlers with a single JSON stdout handler."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactSecretsFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Route uvicorn's own loggers through the same JSON handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    # httpx logs full request URLs at INFO; keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
