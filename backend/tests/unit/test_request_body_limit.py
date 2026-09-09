"""Unit tests for request-body enforcement before multipart parsing."""

import asyncio
import json
from collections.abc import Sequence
from typing import cast

from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

from investment_research.api.middleware import (
    MULTIPART_OVERHEAD_ALLOWANCE_BYTES,
    RequestBodyLimitMiddleware,
)


def run_request(
    *,
    headers: Sequence[tuple[bytes, bytes]] = (),
    body_chunks: Sequence[bytes] = (b"",),
    max_upload_bytes: int = 1024,
) -> tuple[list[Message], int]:
    """Execute one synthetic HTTP request and return response messages and app calls."""

    sent_messages: list[Message] = []
    application_calls = 0
    incoming_messages = iter(
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(body_chunks) - 1,
        }
        for index, chunk in enumerate(body_chunks)
    )

    async def receive() -> Message:
        return cast(Message, next(incoming_messages, {"type": "http.disconnect"}))

    async def send(message: Message) -> None:
        sent_messages.append(message)

    async def application(scope: Scope, receive_body: Receive, send_response: Send) -> None:
        nonlocal application_calls
        application_calls += 1
        while True:
            message = await receive_body()
            if message["type"] != "http.request" or not message.get("more_body", False):
                break
        await Response(status_code=204)(scope, receive_body, send_response)

    scope = cast(
        Scope,
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/documents",
            "raw_path": b"/api/v1/documents",
            "query_string": b"",
            "headers": list(headers),
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
        },
    )
    middleware = RequestBodyLimitMiddleware(
        application,
        max_upload_bytes=max_upload_bytes,
    )
    asyncio.run(middleware(scope, receive, send))
    return sent_messages, application_calls


def response_status(messages: Sequence[Message]) -> int:
    """Return the status code from one captured ASGI response."""

    start = next(message for message in messages if message["type"] == "http.response.start")
    return cast(int, start["status"])


def response_json(messages: Sequence[Message]) -> object:
    """Decode the JSON body from captured ASGI response messages."""

    body = b"".join(
        cast(bytes, message.get("body", b""))
        for message in messages
        if message["type"] == "http.response.body"
    )
    return json.loads(body)


def test_rejects_oversized_declared_content_length_before_application() -> None:
    request_limit = 1024 + MULTIPART_OVERHEAD_ALLOWANCE_BYTES

    messages, application_calls = run_request(
        headers=((b"content-length", str(request_limit + 1).encode()),),
    )

    assert response_status(messages) == 413
    assert response_json(messages) == {
        "error": {
            "code": "PAYLOAD_TOO_LARGE",
            "message": "request body exceeds the configured upload limit",
            "request_id": None,
        }
    }
    assert application_calls == 0


def test_rejects_streamed_body_that_exceeds_limit_without_header() -> None:
    request_limit = 1024 + MULTIPART_OVERHEAD_ALLOWANCE_BYTES

    messages, application_calls = run_request(
        body_chunks=(b"a" * request_limit, b"b"),
    )

    assert response_status(messages) == 413
    assert application_calls == 1


def test_rejects_ambiguous_content_length_headers() -> None:
    messages, application_calls = run_request(
        headers=((b"content-length", b"10"), (b"content-length", b"11")),
    )

    assert response_status(messages) == 400
    assert response_json(messages) == {
        "error": {
            "code": "INVALID_REQUEST",
            "message": "content-length header is invalid",
            "request_id": None,
        }
    }
    assert application_calls == 0


def test_allows_body_at_exact_request_limit() -> None:
    request_limit = 1024 + MULTIPART_OVERHEAD_ALLOWANCE_BYTES

    messages, application_calls = run_request(
        headers=((b"content-length", str(request_limit).encode()),),
        body_chunks=(b"a" * request_limit,),
    )

    assert response_status(messages) == 204
    assert application_calls == 1
