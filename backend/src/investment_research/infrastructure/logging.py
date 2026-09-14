"""Structured application logging with fail-closed field selection."""

import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from types import TracebackType
from typing import IO, override
from uuid import UUID

APPLICATION_LOGGER_NAME = "investment_research"
EVENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
TEXT_FIELD_PATTERN = re.compile(r"^[A-Za-z0-9_./:{}-]{1,256}$")
ALLOWED_EXTRA_FIELDS = (
    "request_id",
    "workspace_id",
    "job_id",
    "trace_id",
    "method",
    "route",
    "status_code",
    "duration_ms",
    "exception_type",
)

_request_id: ContextVar[UUID | None] = ContextVar("request_id", default=None)


def current_request_id() -> UUID | None:
    """Return the server-owned identifier bound to the current request context."""

    return _request_id.get()


@contextmanager
def request_log_context(request_id: UUID) -> Iterator[None]:
    """Bind one request identifier and restore the previous context afterward."""

    token: Token[UUID | None] = _request_id.set(request_id)
    try:
        yield
    finally:
        _request_id.reset(token)


def _safe_event(record: logging.LogRecord) -> str:
    """Return a bounded event name without interpolating arbitrary log arguments."""

    if (
        not isinstance(record.msg, str)
        or record.args
        or not EVENT_NAME_PATTERN.fullmatch(record.msg)
    ):
        return "application.log.redacted"
    return record.msg


def _safe_extra_value(name: str, value: object) -> str | int | float | None:
    """Normalize one allowlisted value or omit it when it is not structurally safe."""

    if value is None:
        return None
    if name in {"request_id", "workspace_id", "job_id"}:
        if isinstance(value, UUID):
            return str(value)
        try:
            return str(UUID(str(value)))
        except AttributeError, TypeError, ValueError:
            return None
    if name == "status_code":
        return value if isinstance(value, int) and 100 <= value <= 599 else None
    if name == "duration_ms":
        return round(float(value), 3) if isinstance(value, int | float) and value >= 0 else None
    if isinstance(value, str) and TEXT_FIELD_PATTERN.fullmatch(value):
        return value
    return None


class SafeJsonFormatter(logging.Formatter):
    """Serialize only stable event names and explicitly allowlisted metadata."""

    def __init__(self, *, process_name: str) -> None:
        super().__init__()
        if not EVENT_NAME_PATTERN.fullmatch(process_name):
            raise ValueError("process_name must be a safe lowercase event token")
        self._process_name = process_name

    def format(self, record: logging.LogRecord) -> str:
        """Return one compact JSON record without message arguments or traceback text."""

        payload: dict[str, str | int | float] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "process": self._process_name,
            "event": _safe_event(record),
        }
        contextual_request_id = current_request_id()
        if contextual_request_id is not None and not hasattr(record, "request_id"):
            payload["request_id"] = str(contextual_request_id)

        for name in ALLOWED_EXTRA_FIELDS:
            safe_value = _safe_extra_value(name, getattr(record, name, None))
            if safe_value is not None:
                payload[name] = safe_value

        if record.exc_info is not None and "exception_type" not in payload:
            exception_class = record.exc_info[0]
            if exception_class is not None:
                payload["exception_type"] = exception_class.__name__

        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    @override
    def formatException(
        self,
        exc_info: tuple[type[BaseException], BaseException, TracebackType | None]
        | tuple[None, None, None],
    ) -> str:
        """Never serialize exception messages or traceback content."""

        del exc_info
        return ""


def configure_logging(
    *,
    process_name: str,
    level: str,
    stream: IO[str] | None = None,
) -> logging.Logger:
    """Configure one non-propagating application logger for the current process."""

    logger = logging.getLogger(APPLICATION_LOGGER_NAME)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SafeJsonFormatter(process_name=process_name))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def application_logger(name: str) -> logging.Logger:
    """Return a child logger inside the configured application namespace."""

    if not name or name.startswith("."):
        raise ValueError("logger name must be a non-empty relative name")
    return logging.getLogger(f"{APPLICATION_LOGGER_NAME}.{name}")
