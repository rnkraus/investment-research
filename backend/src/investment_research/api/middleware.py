"""Transport-level safeguards applied before FastAPI parses request bodies."""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from investment_research.api.contracts import ErrorCode, ErrorDetail, ErrorResponse

MULTIPART_OVERHEAD_ALLOWANCE_BYTES = 64 * 1024


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

        error = ErrorResponse(error=ErrorDetail(code=code, message=message))
        response = JSONResponse(
            status_code=status_code,
            content=error.model_dump(mode="json"),
            headers={"connection": "close"},
        )
        await response(scope, receive, send)
