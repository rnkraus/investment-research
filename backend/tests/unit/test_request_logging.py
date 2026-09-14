"""Tests for server-owned request correlation and safe HTTP events."""

import json
from io import StringIO
from typing import cast
from uuid import UUID

from fastapi import APIRouter, FastAPI
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from investment_research.api.middleware import RequestLoggingMiddleware
from investment_research.infrastructure.logging import configure_logging

SECRET_MARKER = "synthetic-secret-marker"


def read_log(stream: StringIO) -> dict[str, object]:
    """Decode the single structured request record written to a test stream."""

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    return cast(dict[str, object], json.loads(lines[0]))


def test_request_logging_uses_server_id_and_route_template() -> None:
    async def read_document(request: Request) -> JSONResponse:
        del request
        return JSONResponse({"status": "ok"})

    stream = StringIO()
    configure_logging(process_name="api", level="INFO", stream=stream)
    inner_application = FastAPI()
    inner_application.add_api_route("/documents/{document_id}", read_document)
    application = RequestLoggingMiddleware(
        inner_application,
        routes=inner_application.routes,
    )

    with TestClient(application) as client:
        response = client.get(
            f"/documents/{SECRET_MARKER}?token={SECRET_MARKER}",
            headers={"x-request-id": SECRET_MARKER},
        )

    request_id = UUID(response.headers["x-request-id"])
    payload = read_log(stream)
    assert response.status_code == 200
    assert payload["event"] == "http.request.completed"
    assert payload["request_id"] == str(request_id)
    assert payload["method"] == "GET"
    assert payload["route"] == "/documents/{document_id}"
    assert payload["status_code"] == 200
    assert SECRET_MARKER not in stream.getvalue()


def test_request_logging_resolves_included_fastapi_route_template() -> None:
    async def read_document(request: Request) -> JSONResponse:
        del request
        return JSONResponse({"status": "ok"})

    stream = StringIO()
    configure_logging(process_name="api", level="INFO", stream=stream)
    router = APIRouter()
    router.add_api_route("/documents/{document_id}", read_document)
    inner_application = FastAPI()
    inner_application.include_router(router, prefix="/api/v1")
    application = RequestLoggingMiddleware(
        inner_application,
        routes=inner_application.routes,
    )

    with TestClient(application) as client:
        response = client.get(f"/api/v1/documents/{SECRET_MARKER}")

    payload = read_log(stream)
    assert response.status_code == 200
    assert payload["route"] == "/api/v1/documents/{document_id}"
    assert SECRET_MARKER not in stream.getvalue()


def test_request_logging_does_not_emit_an_unmatched_raw_path() -> None:
    stream = StringIO()
    configure_logging(process_name="api", level="INFO", stream=stream)
    inner_application = FastAPI()
    application = RequestLoggingMiddleware(
        inner_application,
        routes=inner_application.routes,
    )

    with TestClient(application) as client:
        response = client.get(f"/unknown/{SECRET_MARKER}")

    payload = read_log(stream)
    assert response.status_code == 404
    assert payload["route"] == "unmatched"
    assert SECRET_MARKER not in stream.getvalue()


def test_request_logging_reports_exception_type_without_details() -> None:
    async def fail(request: Request) -> JSONResponse:
        del request
        raise RuntimeError(f"private failure: {SECRET_MARKER}")

    stream = StringIO()
    configure_logging(process_name="api", level="INFO", stream=stream)
    inner_application = Starlette(routes=[Route("/failure", fail)])
    application = RequestLoggingMiddleware(
        inner_application,
        routes=inner_application.routes,
    )

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/failure")

    payload = read_log(stream)
    assert response.status_code == 500
    assert payload["event"] == "http.request.failed"
    assert payload["route"] == "/failure"
    assert payload["exception_type"] == "RuntimeError"
    assert SECRET_MARKER not in stream.getvalue()
    assert "private failure" not in stream.getvalue()
