"""Unit tests for bounded, application-owned document parser contracts."""

from io import BytesIO
from typing import BinaryIO

import pytest
from pydantic import ValidationError

from investment_research.modules.processing.contracts import (
    MAX_CONFIGURABLE_EXTRACTED_CHARACTERS,
    MAX_CONFIGURABLE_PDF_PAGES,
    ParsedDocument,
    ParsedPage,
    ParserLimits,
)
from investment_research.modules.processing.ports import DocumentParser


class FakeDocumentParser:
    """Minimal structural implementation used to verify the parser port."""

    @property
    def parser_version(self) -> str:
        """Identify the deterministic fake implementation."""

        return "fake-parser-1"

    def parse(self, content: BinaryIO, *, limits: ParserLimits) -> ParsedDocument:
        """Return fixed page text after observing the caller-owned inputs."""

        assert content.read() == b"%PDF-synthetic"
        assert limits.max_pages == 10
        return ParsedDocument(pages=(ParsedPage(page_number=1, text="Revenue grew."),))


def test_parser_limits_accept_positive_bounded_values() -> None:
    limits = ParserLimits(
        max_pages=25,
        max_extracted_characters=100_000,
    )

    assert limits.max_pages == 25
    assert limits.max_extracted_characters == 100_000


@pytest.mark.parametrize(
    ("field", "value", "expected_error_type"),
    [
        pytest.param("max_pages", 0, "greater_than_equal", id="zero-pages"),
        pytest.param(
            "max_pages",
            MAX_CONFIGURABLE_PDF_PAGES + 1,
            "less_than_equal",
            id="too-many-pages",
        ),
        pytest.param(
            "max_extracted_characters",
            0,
            "greater_than_equal",
            id="zero-characters",
        ),
        pytest.param(
            "max_extracted_characters",
            MAX_CONFIGURABLE_EXTRACTED_CHARACTERS + 1,
            "less_than_equal",
            id="too-many-characters",
        ),
    ],
)
def test_parser_limits_reject_out_of_range_values(
    field: str,
    value: int,
    expected_error_type: str,
) -> None:
    payload = {
        "max_pages": 25,
        "max_extracted_characters": 100_000,
        field: value,
    }

    with pytest.raises(ValidationError) as error:
        ParserLimits.model_validate(payload)

    assert error.value.errors()[0]["type"] == expected_error_type


def test_parsed_document_preserves_ordered_physical_pages() -> None:
    document = ParsedDocument(
        pages=(
            ParsedPage(page_number=1, text="First page."),
            ParsedPage(page_number=2, text=""),
            ParsedPage(page_number=3, text="Third page."),
        )
    )

    assert document.page_count == 3
    assert document.extracted_character_count == len("First page.Third page.")
    assert document.pages[1].text == ""
    assert set(document.model_dump()) == {"pages"}


@pytest.mark.parametrize(
    "pages",
    [
        pytest.param((), id="no-pages"),
        pytest.param(
            (
                ParsedPage(page_number=1, text="First"),
                ParsedPage(page_number=3, text="Third"),
            ),
            id="page-number-gap",
        ),
        pytest.param(
            (
                ParsedPage(page_number=2, text="Second"),
                ParsedPage(page_number=1, text="First"),
            ),
            id="out-of-order",
        ),
    ],
)
def test_parsed_document_rejects_invalid_page_sequences(
    pages: tuple[ParsedPage, ...],
) -> None:
    with pytest.raises(ValidationError):
        ParsedDocument(pages=pages)


def test_parser_contracts_are_frozen_and_reject_unknown_fields() -> None:
    page = ParsedPage(page_number=1, text="Exact text")

    with pytest.raises(ValidationError) as frozen_error:
        page.text = "Changed text"
    with pytest.raises(ValidationError) as extra_error:
        ParsedPage.model_validate({"page_number": 1, "text": "Exact text", "unexpected": True})

    assert frozen_error.value.errors()[0]["type"] == "frozen_instance"
    assert extra_error.value.errors()[0]["type"] == "extra_forbidden"


def test_document_parser_port_accepts_an_application_owned_implementation() -> None:
    parser: DocumentParser = FakeDocumentParser()
    content = BytesIO(b"%PDF-synthetic")
    limits = ParserLimits(max_pages=10, max_extracted_characters=1_000)

    result = parser.parse(content, limits=limits)

    assert parser.parser_version == "fake-parser-1"
    assert result.pages == (ParsedPage(page_number=1, text="Revenue grew."),)
    assert content.closed is False
