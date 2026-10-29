"""Allowlisted structured logging; arbitrary messages and extras are discarded."""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

Event = Literal["application_started", "health_checked"]
_ALLOWED_EVENTS = frozenset(
    {
        "application_started",
        "health_checked",
        "execution_started",
        "execution_completed",
        "execution_failed",
        "retry_scheduled",
        "telemetry_failed",
    }
)
_ALLOWED_ERRORS = frozenset(
    {
        "invalid_input",
        "unauthorized",
        "not_found",
        "conflict",
        "configuration",
        "invalid_output",
        "timeout",
        "rate_limited",
        "unavailable",
        "storage_busy",
        "integrity",
        "cancelled",
        "limit_exceeded",
        "unknown_outcome",
        "internal",
    }
)


class SafeJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Never interpolate record.msg/args or serialize exceptions or arbitrary extras.
        candidate = getattr(record, "event", None)
        event = (
            candidate if isinstance(candidate, str) and candidate in _ALLOWED_EVENTS else "event"
        )
        data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": logging.getLevelName(record.levelno),
            "event": event,
        }
        for name in ("correlation_id", "workflow_id", "operation_id"):
            value = getattr(record, name, None)
            if isinstance(value, (UUID, str)):
                try:
                    data[name] = str(UUID(str(value)))
                except ValueError:
                    pass
        code = getattr(record, "error_code", None)
        if isinstance(code, str) and code in _ALLOWED_ERRORS:
            data["error_code"] = code
        return json.dumps(data)


def configure_logging(level: str) -> logging.Logger:
    """Configure only our logger, leaving host/root logging unchanged."""
    logger = logging.getLogger("careerpilot")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(SafeJsonFormatter())
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def log_event(logger: logging.Logger, event: Event) -> None:
    logger.info("", extra={"event": event})
