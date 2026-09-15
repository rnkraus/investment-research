"""Unit tests for the private credential-free parser application."""

from dataclasses import dataclass, field
from typing import BinaryIO, Protocol, cast

import pytest
from fastapi.testclient import TestClient

from investment_research.infrastructure.parsing.http_protocol import (
    PARSER_HEALTH_PATH,
    PARSER_HTTP_PATH,
    PARSER_REQUEST_CONTENT_TYPE,
    PARSER_REQUEST_METADATA_HEADER,
)
from investment_research.modules.processing.contracts import (
    PDF_PARSER_CONFIG_VERSION_V1,
    ParsedDocument,
    ParsedPage,
    ParserFailureV1,
    ParserLimits,
    ParserRequestV1,
    ParserResponseV1,
)
from investment_research.modules.processing.ports import (
    DocumentParserError,
    ParserLimitExceededError,
)
from investment_research.parser_app import create_parser_app
from investment_research.settings import ParserSettings

PARSER_VERSION = "synthetic-parser-1"
PDF_BYTES = b"%PDF-synthetic"
LIMITS = ParserLimits(max_pages=10, max_extracted_characters=1_000)


class HttpResponseView(Protocol):
    """Small structural view of the test client's runtime response."""

    status_code: int
    text: str

    def json(self) -> object:
        """Decode the response body for contract validation."""

        ...


@dataclass(slots=True)
class ControllableParser:
    """Return one configured result or raise one configured safe test error."""

    result: ParsedDocument = field(
        default_factory=lambda: ParsedDocument(
            pages=(ParsedPage(page_number=1, text="Revenue grew."),)
        )
    )
    error: Exception | None = None
    calls: list[tuple[bytes, ParserLimits]] = field(default_factory=list)

    @property
    def parser_version(self) -> str:
        return PARSER_VERSION

    def parse(self, content: BinaryIO, *, limits: ParserLimits) -> ParsedDocument:
        self.calls.append((content.read(), limits))
        if self.error is not None:
            raise self.error
        return self.result


def make_settings(*, max_request_bytes: int = 1_024) -> ParserSettings:
    return ParserSettings(max_request_bytes=max_request_bytes)


def metadata_header(
    *,
    limits: ParserLimits = LIMITS,
    parser_version: str = PARSER_VERSION,
    parser_config_version: str = PDF_PARSER_CONFIG_VERSION_V1,
) -> dict[str, str]:
    metadata = ParserRequestV1(
        parser_version=parser_version,
        parser_config_version=parser_config_version,
        limits=limits,
    )
    return {PARSER_REQUEST_METADATA_HEADER: metadata.model_dump_json()}


def post_pdf(
    client: TestClient,
    *,
    content: bytes = PDF_BYTES,
    headers: dict[str, str] | None = None,
) -> HttpResponseView:
    request_headers = {"Content-Type": PARSER_REQUEST_CONTENT_TYPE, **(headers or {})}
    return cast(
        HttpResponseView,
        client.post(PARSER_HTTP_PATH, content=content, headers=request_headers),
    )


def test_health_reports_listener_readiness_without_parsing() -> None:
    parser = ControllableParser()
    client = TestClient(create_parser_app(make_settings(), parser))

    response = client.get(PARSER_HEALTH_PATH)

    assert response.status_code == 200
    assert response.text == "ok"
    assert parser.calls == []


def test_success_returns_versioned_bounded_document() -> None:
    parser = ControllableParser()
    client = TestClient(create_parser_app(make_settings(), parser))

    response = post_pdf(client, headers=metadata_header())
    result = ParserResponseV1.model_validate(response.json())

    assert response.status_code == 200
    assert result.parser_version == PARSER_VERSION
    assert result.parser_config_version == PDF_PARSER_CONFIG_VERSION_V1
    assert result.document == parser.result
    assert parser.calls == [(PDF_BYTES, LIMITS)]


@pytest.mark.parametrize(
    ("headers", "content_type"),
    [
        pytest.param({}, PARSER_REQUEST_CONTENT_TYPE, id="missing-metadata"),
        pytest.param(metadata_header(), "text/plain", id="wrong-content-type"),
        pytest.param(
            metadata_header(parser_version="different-parser-1"),
            PARSER_REQUEST_CONTENT_TYPE,
            id="parser-version-mismatch",
        ),
        pytest.param(
            metadata_header(parser_config_version="different-config-1"),
            PARSER_REQUEST_CONTENT_TYPE,
            id="parser-config-mismatch",
        ),
    ],
)
def test_invalid_request_is_rejected_before_parser_execution(
    headers: dict[str, str],
    content_type: str,
) -> None:
    parser = ControllableParser()
    client = TestClient(create_parser_app(make_settings(), parser))

    response = client.post(
        PARSER_HTTP_PATH,
        content=PDF_BYTES,
        headers={"Content-Type": content_type, **headers},
    )

    assert response.status_code in {400, 415}
    assert ParserFailureV1.model_validate(response.json()).code == "invalid_request"
    assert parser.calls == []


def test_oversized_request_is_rejected_before_parser_execution() -> None:
    parser = ControllableParser()
    client = TestClient(create_parser_app(make_settings(), parser))

    response = post_pdf(
        client,
        content=b"x" * 1_025,
        headers=metadata_header(),
    )

    assert response.status_code == 413
    assert ParserFailureV1.model_validate(response.json()).code == "request_too_large"
    assert parser.calls == []


@pytest.mark.parametrize(
    ("parser_error", "expected_code", "expected_status"),
    [
        pytest.param(
            ParserLimitExceededError("sensitive limit details"),
            "limit_exceeded",
            422,
            id="parser-limit",
        ),
        pytest.param(
            DocumentParserError("sensitive parser details"),
            "parse_failed",
            422,
            id="document-error",
        ),
        pytest.param(
            RuntimeError("sensitive implementation details"),
            "internal_error",
            500,
            id="unexpected-error",
        ),
    ],
)
def test_parser_failures_return_only_safe_categories(
    parser_error: Exception,
    expected_code: str,
    expected_status: int,
) -> None:
    parser = ControllableParser(error=parser_error)
    client = TestClient(create_parser_app(make_settings(), parser))

    response = post_pdf(client, headers=metadata_header())

    assert response.status_code == expected_status
    assert ParserFailureV1.model_validate(response.json()).code == expected_code
    assert "sensitive" not in response.text


def test_parser_output_cannot_exceed_requested_document_limits() -> None:
    parser = ControllableParser(
        result=ParsedDocument(pages=(ParsedPage(page_number=1, text="too long"),))
    )
    client = TestClient(create_parser_app(make_settings(), parser))
    limits = ParserLimits(max_pages=1, max_extracted_characters=1)

    response = post_pdf(client, headers=metadata_header(limits=limits))

    assert response.status_code == 500
    assert ParserFailureV1.model_validate(response.json()).code == "response_too_large"
