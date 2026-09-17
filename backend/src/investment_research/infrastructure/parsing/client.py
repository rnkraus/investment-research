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
    PARSER_V2_HTTP_PATH,
)
from investment_research.modules.processing.contracts import (
    M1_PARSER_CONFIG_VERSION,
    ParsedDocument,
    ParsedDocumentV2,
    ParserFailureV1,
    ParserFailureV2,
    ParserLimits,
    ParserMediaType,
    ParserRequestV1,
    ParserRequestV2,
    ParserResponseV1,
    ParserResponseV2,
    parser_response_byte_limit,
    parser_response_v2_byte_limit,
)
from investment_research.modules.processing.domain import ConfigurationVersion
from investment_research.modules.processing.ports import (
    DocumentParserError,
    ParsedDocumentResult,
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

    def parse(
        self,
        content: BinaryIO,
        *,
        limits: ParserLimits,
        media_type: ParserMediaType = "application/pdf",
    ) -> ParsedDocumentResult:
        """Send one bounded typed original and validate the complete response."""

        request_body = self._read_request_body(content)
        if self.parser_config_version == M1_PARSER_CONFIG_VERSION:
            v2_metadata = ParserRequestV2(
                media_type=media_type,
                parser_version=self.parser_version,
                parser_config_version=self.parser_config_version,
                max_input_bytes=self.max_request_bytes,
                max_extracted_characters=limits.max_extracted_characters,
                max_pdf_pages=limits.max_pages if media_type == "application/pdf" else None,
            )
            metadata: ParserRequestV1 | ParserRequestV2 = v2_metadata
            path = PARSER_V2_HTTP_PATH
            content_type = media_type
            maximum_response_bytes = parser_response_v2_byte_limit(v2_metadata)
        else:
            if media_type != "application/pdf":
                raise ParserProtocolError("parser protocol v1 accepts only PDF originals")
            metadata = ParserRequestV1(
                parser_version=self.parser_version,
                parser_config_version=self.parser_config_version,
                limits=limits,
            )
            path = PARSER_HTTP_PATH
            content_type = PARSER_REQUEST_CONTENT_TYPE
            maximum_response_bytes = parser_response_byte_limit(limits)
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
                path,
                body=request_body,
                headers={
                    "Accept": PARSER_RESPONSE_CONTENT_TYPE,
                    "Content-Type": content_type,
                    PARSER_REQUEST_METADATA_HEADER: metadata_json,
                },
            )
            response = connection.getresponse()
            response_body = self._read_response_body(response, maximum_response_bytes)
            self._require_json_content_type(response)
            if response.status == 200:
                if isinstance(metadata, ParserRequestV2):
                    return self._validate_success_v2(
                        response_body,
                        metadata,
                        request_body,
                    )
                return self._validate_success(response_body, metadata)
            self._raise_parser_failure(
                response_body,
                v2=isinstance(metadata, ParserRequestV2),
            )
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
        raise ParserLimitExceededError("original exceeds the parser request-size limit")

    @staticmethod
    def _read_response_body(response: HTTPResponse, maximum_bytes: int) -> bytes:
        """Reject a declared or observed response above the deterministic bound."""

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
    def _validate_success_v2(
        body: bytes,
        request: ParserRequestV2,
        original_body: bytes,
    ) -> ParsedDocumentV2:
        """Validate versions, bounds, media kind, and exact text-source slices."""

        try:
            response = ParserResponseV2.model_validate_json(body)
        except ValidationError:
            raise ParserProtocolError("parser response is invalid") from None
        if (
            response.parser_version != request.parser_version
            or response.parser_config_version != request.parser_config_version
        ):
            raise ParserProtocolError("parser response version does not match the request")

        document = response.document
        if document.extracted_character_count > request.max_extracted_characters:
            raise ParserProtocolError("parser response exceeds the requested document limits")
        if any(len(block.text) > request.max_block_characters for block in document.blocks):
            raise ParserProtocolError("parser response exceeds the requested block limit")
        if request.media_type == "application/pdf":
            if (
                document.kind != "PDF"
                or request.max_pdf_pages is None
                or document.physical_page_count > request.max_pdf_pages
            ):
                raise ParserProtocolError("parser response does not match the PDF request")
            return document

        if document.kind != "PASTED_TEXT":
            raise ParserProtocolError("parser response does not match the text request")
        try:
            canonical_text = original_body.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ParserProtocolError("plain-text request is not valid UTF-8") from None
        for block in document.blocks:
            if canonical_text[block.start_character : block.end_character] != block.text:
                raise ParserProtocolError("parser response does not match the text original")
        return document

    @staticmethod
    def _raise_parser_failure(body: bytes, *, v2: bool = False) -> None:
        """Map the parser's bounded failure category to an application-owned error."""

        try:
            failure = (
                ParserFailureV2.model_validate_json(body)
                if v2
                else ParserFailureV1.model_validate_json(body)
            )
        except ValidationError:
            raise ParserProtocolError("parser failure response is invalid") from None
        if failure.code in {"request_too_large", "limit_exceeded", "response_too_large"}:
            raise ParserLimitExceededError("isolated parser safety limit exceeded")
        if failure.code == "parse_failed":
            raise DocumentParserError("isolated parser rejected the document")
        if failure.code == "internal_error":
            raise ParserTransportError("isolated parser failed internally")
        if failure.code == "timeout":
            raise ParserTransportError("isolated parser exceeded its deadline")
        raise ParserProtocolError("isolated parser rejected the request")
