"""Bounded HTTP adapter from the trusted worker to the isolated parser."""

from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException, HTTPResponse
from typing import BinaryIO
from urllib.parse import urlsplit

from pydantic import ValidationError

from investment_research.infrastructure.parsing.http_protocol import (
    MAX_PARSER_REQUEST_METADATA_BYTES,
    PARSER_HTTP_PATH,
    PARSER_REQUEST_CONTENT_TYPE,
    PARSER_REQUEST_METADATA_HEADER,
    PARSER_RESPONSE_CONTENT_TYPE,
)
from investment_research.modules.processing.contracts import (
    ParsedDocument,
    ParserFailureV1,
    ParserLimits,
    ParserRequestV1,
    ParserResponseV1,
    parser_response_byte_limit,
)
from investment_research.modules.processing.domain import ConfigurationVersion
from investment_research.modules.processing.ports import (
    DocumentParserError,
    ParserLimitExceededError,
    ParserProtocolError,
    ParserTransportError,
)

READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class HttpDocumentParser:
    """Call one fixed private parser endpoint through the `DocumentParser` port."""

    endpoint_url: str
    parser_version: ConfigurationVersion
    parser_config_version: ConfigurationVersion
    timeout_seconds: int
    max_request_bytes: int

    def __post_init__(self) -> None:
        """Reject unsafe direct construction outside validated settings."""

        endpoint = urlsplit(self.endpoint_url)
        if (
            endpoint.scheme != "http"
            or endpoint.hostname is None
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path not in {"", "/"}
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("parser endpoint must be one credential-free HTTP origin")
        if self.timeout_seconds < 1:
            raise ValueError("parser timeout must be positive")
        if self.max_request_bytes < 1:
            raise ValueError("parser request limit must be positive")

    def parse(self, content: BinaryIO, *, limits: ParserLimits) -> ParsedDocument:
        """Send bounded PDF bytes and validate one complete parser response."""

        request_body = self._read_request_body(content)
        metadata = ParserRequestV1(
            parser_version=self.parser_version,
            parser_config_version=self.parser_config_version,
            limits=limits,
        )
        metadata_json = metadata.model_dump_json()
        if len(metadata_json.encode("ascii")) > MAX_PARSER_REQUEST_METADATA_BYTES:
            raise ParserProtocolError("parser request metadata exceeds its protocol limit")

        endpoint = urlsplit(self.endpoint_url)
        host = endpoint.hostname
        if host is None:
            raise ParserProtocolError("parser endpoint has no host")
        connection = HTTPConnection(
            host,
            endpoint.port or 80,
            timeout=self.timeout_seconds,
        )
        response: HTTPResponse | None = None
        try:
            connection.request(
                "POST",
                PARSER_HTTP_PATH,
                body=request_body,
                headers={
                    "Accept": PARSER_RESPONSE_CONTENT_TYPE,
                    "Content-Type": PARSER_REQUEST_CONTENT_TYPE,
                    PARSER_REQUEST_METADATA_HEADER: metadata_json,
                },
            )
            response = connection.getresponse()
            response_body = self._read_response_body(response, limits)
            self._require_json_content_type(response)
            if response.status == 200:
                return self._validate_success(response_body, metadata)
            self._raise_parser_failure(response_body)
        except (TimeoutError, OSError, HTTPException) as error:
            raise ParserTransportError("isolated parser transport failed") from error
        finally:
            if response is not None:
                response.close()
            connection.close()

        raise AssertionError("parser failure handling returned unexpectedly")

    def _read_request_body(self, content: BinaryIO) -> bytes:
        """Read at most one configured request plus a single overflow byte."""

        body = bytearray()
        while len(body) <= self.max_request_bytes:
            remaining = self.max_request_bytes + 1 - len(body)
            chunk = content.read(min(READ_CHUNK_BYTES, remaining))
            if not chunk:
                return bytes(body)
            body.extend(chunk)
        raise ParserLimitExceededError("PDF exceeds the parser request-size limit")

    @staticmethod
    def _read_response_body(response: HTTPResponse, limits: ParserLimits) -> bytes:
        """Reject a declared or observed response above the deterministic bound."""

        maximum_bytes = parser_response_byte_limit(limits)
        declared_length = response.getheader("Content-Length")
        if declared_length is not None:
            try:
                if int(declared_length) > maximum_bytes:
                    raise ParserProtocolError("parser response exceeds its protocol limit")
            except ValueError as error:
                raise ParserProtocolError(
                    "parser response has an invalid content length"
                ) from error

        body = response.read(maximum_bytes + 1)
        if len(body) > maximum_bytes:
            raise ParserProtocolError("parser response exceeds its protocol limit")
        return body

    @staticmethod
    def _require_json_content_type(response: HTTPResponse) -> None:
        """Require the exact safe response representation before decoding it."""

        content_type = response.getheader("Content-Type", "").partition(";")[0].strip().lower()
        if content_type != PARSER_RESPONSE_CONTENT_TYPE:
            raise ParserProtocolError("parser response has an invalid content type")

    @staticmethod
    def _validate_success(body: bytes, request: ParserRequestV1) -> ParsedDocument:
        """Validate schema, versions, and caller-requested output bounds."""

        try:
            response = ParserResponseV1.model_validate_json(body)
        except ValidationError:
            raise ParserProtocolError("parser response is invalid") from None
        if (
            response.parser_version != request.parser_version
            or response.parser_config_version != request.parser_config_version
        ):
            raise ParserProtocolError("parser response version does not match the request")
        if (
            response.document.page_count > request.limits.max_pages
            or response.document.extracted_character_count > request.limits.max_extracted_characters
        ):
            raise ParserProtocolError("parser response exceeds the requested document limits")
        return response.document

    @staticmethod
    def _raise_parser_failure(body: bytes) -> None:
        """Map the parser's bounded failure category to an application-owned error."""

        try:
            failure = ParserFailureV1.model_validate_json(body)
        except ValidationError:
            raise ParserProtocolError("parser failure response is invalid") from None
        if failure.code in {"request_too_large", "limit_exceeded", "response_too_large"}:
            raise ParserLimitExceededError("isolated parser safety limit exceeded")
        if failure.code == "parse_failed":
            raise DocumentParserError("isolated parser rejected the document")
        if failure.code == "internal_error":
            raise ParserTransportError("isolated parser failed internally")
        raise ParserProtocolError("isolated parser rejected the request")
