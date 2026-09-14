"""Tests for fail-closed structured application logging."""

import json
from io import StringIO
from typing import cast
from uuid import UUID

import pytest

from investment_research.infrastructure.logging import (
    application_logger,
    configure_logging,
    current_request_id,
    request_log_context,
)

REQUEST_ID = UUID("00000000-0000-4000-8000-000000000101")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000102")
SECRET_MARKER = "synthetic-secret-marker"


def read_log(stream: StringIO) -> dict[str, object]:
    """Decode the single structured record written to a test stream."""

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    return cast(dict[str, object], json.loads(lines[0]))


def test_logging_emits_only_stable_fields_and_context() -> None:
    stream = StringIO()
    configure_logging(process_name="api", level="INFO", stream=stream)
    logger = application_logger("api.requests")

    with request_log_context(REQUEST_ID):
        logger.info(
            "http.request.completed",
            extra={
                "workspace_id": WORKSPACE_ID,
                "method": "GET",
                "route": "/api/v1/documents/{document_id}",
                "status_code": 200,
                "duration_ms": 1.23456,
                "password": SECRET_MARKER,
            },
        )

    payload = read_log(stream)
    assert payload["event"] == "http.request.completed"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "investment_research.api.requests"
    assert payload["process"] == "api"
    assert payload["request_id"] == str(REQUEST_ID)
    assert payload["workspace_id"] == str(WORKSPACE_ID)
    assert payload["method"] == "GET"
    assert payload["route"] == "/api/v1/documents/{document_id}"
    assert payload["status_code"] == 200
    assert payload["duration_ms"] == 1.235
    assert "timestamp" in payload
    assert "password" not in payload
    assert SECRET_MARKER not in stream.getvalue()
    assert current_request_id() is None


def test_logging_redacts_interpolated_messages_and_arguments() -> None:
    stream = StringIO()
    configure_logging(process_name="worker", level="INFO", stream=stream)

    application_logger("processing").info("unsafe value: %s", SECRET_MARKER)

    payload = read_log(stream)
    assert payload["event"] == "application.log.redacted"
    assert SECRET_MARKER not in stream.getvalue()


def test_logging_keeps_exception_type_without_exception_details() -> None:
    stream = StringIO()
    configure_logging(process_name="worker", level="INFO", stream=stream)
    logger = application_logger("processing")

    try:
        raise RuntimeError(f"private failure: {SECRET_MARKER}")
    except RuntimeError:
        logger.exception("processing.failed")

    payload = read_log(stream)
    assert payload["event"] == "processing.failed"
    assert payload["exception_type"] == "RuntimeError"
    assert SECRET_MARKER not in stream.getvalue()
    assert "private failure" not in stream.getvalue()
    assert "Traceback" not in stream.getvalue()


def test_logging_omits_malformed_allowlisted_values() -> None:
    stream = StringIO()
    configure_logging(process_name="api", level="INFO", stream=stream)

    application_logger("api.requests").info(
        "http.request.completed",
        extra={
            "request_id": "not-a-uuid",
            "route": f"/unsafe?token={SECRET_MARKER}",
            "status_code": 999,
            "duration_ms": -1,
        },
    )

    payload = read_log(stream)
    assert "request_id" not in payload
    assert "route" not in payload
    assert "status_code" not in payload
    assert "duration_ms" not in payload
    assert SECRET_MARKER not in stream.getvalue()


def test_logging_rejects_unsafe_process_and_logger_names() -> None:
    with pytest.raises(ValueError, match="process_name"):
        configure_logging(process_name="API with spaces", level="INFO")

    with pytest.raises(ValueError, match="logger name"):
        application_logger("")
