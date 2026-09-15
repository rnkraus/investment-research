"""Unit tests for the bounded worker-to-parser HTTP adapter."""

from dataclasses import dataclass, field
from http.client import HTTPResponse
from io import BytesIO
from typing import ClassVar, cast

import pytest

from investment_research.infrastructure.parsing import client as client_module
from investment_research.infrastructure.parsing.client import HttpDocumentParser
from investment_research.infrastructure.parsing.http_protocol import (
    PARSER_HTTP_PATH,
    PARSER_REQUEST_CONTENT_TYPE,
    PARSER_REQUEST_METADATA_HEADER,
)
from investment_research.modules.processing.contracts import (
    PDF_PARSER_CONFIG_VERSION_V1,
    ParsedDocument,
    ParsedPage,
    ParserFailureCode,
    ParserFailureV1,
    ParserLimits,
    ParserRequestV1,
    ParserResponseV1,
)
from investment_research.modules.processing.ports import (
    DocumentParserError,
    ParserLimitExceededError,
    ParserProtocolError,
    ParserTransportError,
)

PARSER_VERSION = "synthetic-parser-1"
LIMITS = ParserLimits(max_pages=10, max_extracted_characters=1_000)
PDF_BYTES = b"%PDF-synthetic"


@dataclass(slots=True)
class FakeHttpResponse:
    status: int
    body: bytes
    content_type: str = "application/json"
    closed: bool = False

    def getheader(self, name: str, default: str | None = None) -> str | None:
        if name.casefold() == "content-type":
            return self.content_type
        if name.casefold() == "content-length":
            return str(len(self.body))
        return default

    def read(self, amount: int | None = None) -> bytes:
        return self.body if amount is None else self.body[:amount]

    def close(self) -> None:
        self.closed = True


@dataclass(slots=True)
class FakeHttpConnection:
    host: str
    port: int
    timeout: int
    response: FakeHttpResponse | None = None
    request_error: Exception | None = None
    requests: list[tuple[str, str, bytes, dict[str, str]]] = field(default_factory=list)
    closed: bool = False

    next_response: ClassVar[FakeHttpResponse | None] = None
    next_request_error: ClassVar[Exception | None] = None
    instances: ClassVar[list[FakeHttpConnection]] = []

    def __post_init__(self) -> None:
        self.response = type(self).next_response
        self.request_error = type(self).next_request_error
        type(self).instances.append(self)

    def request(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        if self.request_error is not None:
            raise self.request_error
        self.requests.append((method, path, body, headers))

    def getresponse(self) -> HTTPResponse:
        if self.response is None:
            raise AssertionError("fake response was not configured")
        return cast(HTTPResponse, self.response)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def fake_http_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttpConnection.next_response = None
    FakeHttpConnection.next_request_error = None
    FakeHttpConnection.instances = []
    monkeypatch.setattr(client_module, "HTTPConnection", FakeHttpConnection)


def make_parser(*, max_request_bytes: int = 1_024) -> HttpDocumentParser:
    return HttpDocumentParser(
        endpoint_url="http://127.0.0.1:8100",
        parser_version=PARSER_VERSION,
        parser_config_version=PDF_PARSER_CONFIG_VERSION_V1,
        timeout_seconds=30,
        max_request_bytes=max_request_bytes,
    )


def success_response(
    *,
    parser_version: str = PARSER_VERSION,
    text: str = "Revenue grew.",
) -> FakeHttpResponse:
    return FakeHttpResponse(
        status=200,
        body=ParserResponseV1(
            parser_version=parser_version,
            parser_config_version=PDF_PARSER_CONFIG_VERSION_V1,
            document=ParsedDocument(pages=(ParsedPage(page_number=1, text=text),)),
        )
        .model_dump_json()
        .encode("utf-8"),
    )


def test_client_sends_bounded_pdf_and_versioned_metadata() -> None:
    FakeHttpConnection.next_response = success_response()
    parser = make_parser()

    result = parser.parse(BytesIO(PDF_BYTES), limits=LIMITS)

    assert result.pages == (ParsedPage(page_number=1, text="Revenue grew."),)
    connection = FakeHttpConnection.instances[0]
    assert (connection.host, connection.port, connection.timeout) == ("127.0.0.1", 8100, 30)
    method, path, body, headers = connection.requests[0]
    assert (method, path, body) == ("POST", PARSER_HTTP_PATH, PDF_BYTES)
    assert headers["Content-Type"] == PARSER_REQUEST_CONTENT_TYPE
    assert (
        ParserRequestV1.model_validate_json(headers[PARSER_REQUEST_METADATA_HEADER]).limits
        == LIMITS
    )
    assert connection.closed is True
    assert FakeHttpConnection.next_response is not None
    assert FakeHttpConnection.next_response.closed is True


def test_client_rejects_request_before_opening_connection_when_pdf_is_too_large() -> None:
    parser = make_parser(max_request_bytes=1_024)

    with pytest.raises(ParserLimitExceededError, match="request-size limit"):
        parser.parse(BytesIO(b"x" * 1_025), limits=LIMITS)

    assert FakeHttpConnection.instances == []


@pytest.mark.parametrize(
    ("failure_code", "expected_error"),
    [
        pytest.param("limit_exceeded", ParserLimitExceededError, id="limit"),
        pytest.param("parse_failed", DocumentParserError, id="document"),
        pytest.param("internal_error", ParserTransportError, id="internal"),
        pytest.param("invalid_request", ParserProtocolError, id="invalid-request"),
    ],
)
def test_client_maps_safe_parser_failure_categories(
    failure_code: str,
    expected_error: type[Exception],
) -> None:
    FakeHttpConnection.next_response = FakeHttpResponse(
        status=422,
        body=ParserFailureV1(code=cast(ParserFailureCode, failure_code))
        .model_dump_json()
        .encode("utf-8"),
    )

    with pytest.raises(expected_error):
        make_parser().parse(BytesIO(PDF_BYTES), limits=LIMITS)


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(FakeHttpResponse(status=200, body=b"not-json"), id="malformed-json"),
        pytest.param(
            FakeHttpResponse(status=200, body=b"{}", content_type="text/plain"),
            id="wrong-content-type",
        ),
        pytest.param(success_response(parser_version="different-parser-1"), id="wrong-version"),
        pytest.param(success_response(text="x" * 1_001), id="excessive-text"),
    ],
)
def test_client_rejects_untrusted_success_response(response: FakeHttpResponse) -> None:
    FakeHttpConnection.next_response = response

    with pytest.raises(ParserProtocolError):
        make_parser().parse(BytesIO(PDF_BYTES), limits=LIMITS)


def test_client_translates_timeout_and_closes_connection() -> None:
    FakeHttpConnection.next_request_error = TimeoutError("sensitive endpoint details")

    with pytest.raises(ParserTransportError, match="isolated parser transport failed") as error:
        make_parser().parse(BytesIO(PDF_BYTES), limits=LIMITS)

    assert "sensitive" not in str(error.value)
    assert FakeHttpConnection.instances[0].closed is True
