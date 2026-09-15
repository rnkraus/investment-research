"""Credential-free internal application for bounded PDF parsing."""

from io import BytesIO

from fastapi import FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool
from uvicorn import run

from investment_research.infrastructure.parsing.http_protocol import (
    MAX_PARSER_REQUEST_METADATA_BYTES,
    PARSER_HEALTH_PATH,
    PARSER_HTTP_PATH,
    PARSER_REQUEST_CONTENT_TYPE,
    PARSER_REQUEST_METADATA_HEADER,
    PARSER_RESPONSE_CONTENT_TYPE,
)
from investment_research.infrastructure.parsing.pypdf import PypdfDocumentParser
from investment_research.modules.processing.contracts import (
    PDF_PARSER_CONFIG_VERSION_V1,
    ParserFailureCode,
    ParserFailureV1,
    ParserRequestV1,
    ParserResponseV1,
    parser_response_byte_limit,
)
from investment_research.modules.processing.ports import (
    DocumentParser,
    DocumentParserError,
    ParserLimitExceededError,
)
from investment_research.settings import ParserSettings, get_parser_settings

READ_CHUNK_BYTES = 64 * 1024


def _json_response(model: ParserFailureV1 | ParserResponseV1, *, status_code: int) -> Response:
    """Serialize one bounded protocol model with the exact response media type."""

    return Response(
        content=model.model_dump_json(),
        status_code=status_code,
        media_type=PARSER_RESPONSE_CONTENT_TYPE,
    )


def _failure(code: ParserFailureCode, *, status_code: int) -> Response:
    """Return one safe failure category without input or implementation details."""

    return _json_response(ParserFailureV1(code=code), status_code=status_code)


def _parse_metadata(request: Request) -> ParserRequestV1 | None:
    """Validate the small ASCII JSON metadata header without reading the PDF body."""

    raw_metadata = request.headers.get(PARSER_REQUEST_METADATA_HEADER)
    if raw_metadata is None:
        return None
    try:
        encoded_metadata = raw_metadata.encode("ascii")
    except UnicodeEncodeError:
        return None
    if len(encoded_metadata) > MAX_PARSER_REQUEST_METADATA_BYTES:
        return None
    try:
        return ParserRequestV1.model_validate_json(encoded_metadata)
    except ValueError:
        return None


def _declared_request_size(request: Request) -> int | None:
    """Parse one non-negative Content-Length or report invalid input as negative."""

    raw_length = request.headers.get("Content-Length")
    if raw_length is None:
        return None
    try:
        length = int(raw_length)
    except ValueError:
        return -1
    return length if length >= 0 else -1


async def _read_request_body(request: Request, *, maximum_bytes: int) -> bytes | None:
    """Read at most one configured body plus the first observed overflow chunk."""

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum_bytes:
            return None
    return bytes(body)


def create_parser_app(
    settings: ParserSettings | None = None,
    parser: DocumentParser | None = None,
) -> FastAPI:
    """Create the private parser application without loading application settings."""

    active_settings = settings or get_parser_settings()
    active_parser = parser or PypdfDocumentParser()
    application = FastAPI(
        title="Investment Research Internal Parser",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.get(PARSER_HEALTH_PATH, include_in_schema=False)
    async def parser_health() -> Response:
        """Confirm only that the private parser listener can accept requests."""

        return Response(content="ok", media_type="text/plain")

    @application.post(PARSER_HTTP_PATH, include_in_schema=False)
    async def parse_document(request: Request) -> Response:
        content_type = request.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if content_type != PARSER_REQUEST_CONTENT_TYPE:
            return _failure("invalid_request", status_code=415)

        metadata = _parse_metadata(request)
        if metadata is None:
            return _failure("invalid_request", status_code=400)
        if (
            metadata.parser_version != active_parser.parser_version
            or metadata.parser_config_version != PDF_PARSER_CONFIG_VERSION_V1
            or metadata.limits.max_pages > active_settings.max_pdf_pages
            or metadata.limits.max_extracted_characters > active_settings.max_extracted_characters
        ):
            return _failure("invalid_request", status_code=400)

        declared_size = _declared_request_size(request)
        if declared_size is not None and declared_size < 0:
            return _failure("invalid_request", status_code=400)
        if declared_size is not None and declared_size > active_settings.max_request_bytes:
            return _failure("request_too_large", status_code=413)

        body = await _read_request_body(
            request,
            maximum_bytes=active_settings.max_request_bytes,
        )
        if body is None:
            return _failure("request_too_large", status_code=413)

        try:
            document = await run_in_threadpool(
                active_parser.parse,
                BytesIO(body),
                limits=metadata.limits,
            )
        except ParserLimitExceededError:
            return _failure("limit_exceeded", status_code=422)
        except DocumentParserError:
            return _failure("parse_failed", status_code=422)
        except Exception:
            return _failure("internal_error", status_code=500)

        if (
            document.page_count > metadata.limits.max_pages
            or document.extracted_character_count > metadata.limits.max_extracted_characters
        ):
            return _failure("response_too_large", status_code=500)
        response = ParserResponseV1(
            parser_version=active_parser.parser_version,
            parser_config_version=PDF_PARSER_CONFIG_VERSION_V1,
            document=document,
        )
        response_body = response.model_dump_json().encode("utf-8")
        if len(response_body) > parser_response_byte_limit(metadata.limits):
            return _failure("response_too_large", status_code=500)
        return Response(
            content=response_body,
            status_code=200,
            media_type=PARSER_RESPONSE_CONTENT_TYPE,
        )

    return application


def main() -> None:
    """Run the private parser listener from non-secret parser settings only."""

    settings = get_parser_settings()
    run(
        create_parser_app(settings),
        host=settings.parser_host,
        port=settings.parser_port,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
