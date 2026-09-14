"""Transport-level safeguards applied before FastAPI parses request bodies."""

from collections.abc import Sequence
from time import perf_counter
from uuid import uuid4

from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from investment_research.api.contracts import ErrorCode, ErrorDetail, ErrorResponse
from investment_research.infrastructure.logging import (
    application_logger,
    current_request_id,
    request_log_context,
)

MULTIPART_OVERHEAD_ALLOWANCE_BYTES = 64 * 1024
REQUEST_ID_HEADER = b"x-request-id"


def _route_template(scope: Scope, routes: Sequence[BaseRoute]) -> str:
    """Return the matched server-owned route template without the raw request target."""

    partial_match: BaseRoute | None = None
    for route in routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            path_format = _path_format(route)
            if path_format is not None:
                return path_format
            nested_path_format = _included_route_template(scope, route)
            if nested_path_format is not None:
                return nested_path_format
        if match is Match.PARTIAL:
            partial_match = route
    if partial_match is None:
        return "unmatched"
    return (
        _path_format(partial_match) or _included_route_template(scope, partial_match) or "unmatched"
    )


def _path_format(route: BaseRoute) -> str | None:
    """Read a route template only from server-owned routing metadata."""

    path_format = getattr(route, "path_format", getattr(route, "path", None))
    return path_format if isinstance(path_format, str) else None


def _included_route_template(scope: Scope, route: BaseRoute) -> str | None:
    """Resolve FastAPI's optimized included-router wrapper to its concrete route."""

    original_router = getattr(route, "original_router", None)
    nested_routes = getattr(original_router, "routes", ())
    if not isinstance(nested_routes, Sequence):
        return None
    nested_path_format = _route_template(scope, nested_routes)
    if nested_path_format == "unmatched":
        return None

    include_context = getattr(route, "include_context", None)
    prefix = getattr(include_context, "prefix", "")
    if isinstance(prefix, str) and prefix and not nested_path_format.startswith(prefix):
        return f"{prefix}{nested_path_format}"
    return nested_path_format


class RequestLoggingMiddleware:
    """Bind a server-owned request ID and emit one safe completion event."""

    def __init__(self, application: ASGIApp, *, routes: Sequence[BaseRoute]) -> None:
        self._application = application
        self._routes = tuple(routes)
        self._logger = application_logger("api.requests")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._application(scope, receive, send)
            return

        request_id = uuid4()
        started_at = perf_counter()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = [
                    (name, value)
                    for name, value in message.get("headers", ())
                    if name.lower() != REQUEST_ID_HEADER
                ]
                headers.append((REQUEST_ID_HEADER, str(request_id).encode("ascii")))
                message["headers"] = headers
            await send(message)

        with request_log_context(request_id):
            try:
                await self._application(scope, receive, send_with_request_id)
            except Exception as error:
                self._logger.error(
                    "http.request.failed",
                    extra={
                        "method": scope.get("method"),
                        "route": _route_template(scope, self._routes),
                        "status_code": status_code,
                        "duration_ms": (perf_counter() - started_at) * 1000,
                        "exception_type": type(error).__name__,
                    },
                )
                raise
            else:
                self._logger.info(
                    "http.request.completed",
                    extra={
                        "method": scope.get("method"),
                        "route": _route_template(scope, self._routes),
                        "status_code": status_code,
                        "duration_ms": (perf_counter() - started_at) * 1000,
                    },
                )


class RequestBodyTooLargeError(Exception):
    """Raised internally when streamed request bytes cross the configured limit."""


class InvalidContentLengthError(Exception):
    """Raised internally for ambiguous or malformed content-length headers."""


class RequestBodyLimitMiddleware:
    """Reject oversized HTTP bodies before multipart parsing or endpoint execution."""

    def __init__(self, application: ASGIApp, *, max_upload_bytes: int) -> None:
        if max_upload_bytes < 1:
            raise ValueError("max_upload_bytes must be positive")
        self._application = application
        self._max_request_body_bytes = max_upload_bytes + MULTIPART_OVERHEAD_ALLOWANCE_BYTES

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._application(scope, receive, send)
            return

        try:
            content_length = self._content_length(scope)
        except InvalidContentLengthError:
            await self._send_error(
                scope,
                receive,
                send,
                status_code=400,
                code=ErrorCode.INVALID_REQUEST,
                message="content-length header is invalid",
            )
            return

        if content_length is not None and content_length > self._max_request_body_bytes:
            await self._send_too_large(scope, receive, send)
            return

        received_bytes = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self._max_request_body_bytes:
                    raise RequestBodyTooLargeError
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._application(scope, limited_receive, tracked_send)
        except RequestBodyTooLargeError:
            if response_started:
                raise
            await self._send_too_large(scope, receive, send)

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        """Return one unambiguous non-negative content length when supplied."""

        raw_values = [
            value for name, value in scope.get("headers", ()) if name.lower() == b"content-length"
        ]
        if not raw_values:
            return None
        if len(raw_values) != 1:
            raise InvalidContentLengthError
        try:
            content_length = int(raw_values[0].decode("ascii"))
        except (UnicodeDecodeError, ValueError) as error:
            raise InvalidContentLengthError from error
        if content_length < 0:
            raise InvalidContentLengthError
        return content_length

    async def _send_too_large(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Return the stable public oversized-request response."""

        await self._send_error(
            scope,
            receive,
            send,
            status_code=413,
            code=ErrorCode.PAYLOAD_TOO_LARGE,
            message="request body exceeds the configured upload limit",
        )

    @staticmethod
    async def _send_error(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        code: ErrorCode,
        message: str,
    ) -> None:
        """Serialize one stable error envelope without exposing body content."""

        error = ErrorResponse(
            error=ErrorDetail(
                code=code,
                message=message,
                request_id=current_request_id(),
            )
        )
        response = JSONResponse(
            status_code=status_code,
            content=error.model_dump(mode="json"),
            headers={"connection": "close"},
        )
        await response(scope, receive, send)
