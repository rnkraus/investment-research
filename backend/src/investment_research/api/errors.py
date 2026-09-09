"""Stable, non-disclosing HTTP error translation for application failures."""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from investment_research.api.contracts import ErrorCode, ErrorDetail, ErrorResponse


class ApiError(Exception):
    """Expected public failure with a stable status, code, and safe message."""

    def __init__(self, *, status_code: int, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.safe_message = message


async def api_error_handler(request: Request, error: Exception) -> JSONResponse:
    """Serialize an expected API failure without internal exception details."""

    del request
    if not isinstance(error, ApiError):
        raise error
    response = ErrorResponse(error=ErrorDetail(code=error.code, message=error.safe_message))
    return JSONResponse(
        status_code=error.status_code,
        content=response.model_dump(mode="json"),
    )


async def request_validation_error_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """Return one stable response for invalid fields without echoing their values."""

    del request
    if not isinstance(error, RequestValidationError):
        raise error
    response = ErrorResponse(
        error=ErrorDetail(
            code=ErrorCode.INVALID_REQUEST,
            message="request validation failed",
        )
    )
    return JSONResponse(
        status_code=422,
        content=response.model_dump(mode="json"),
    )
