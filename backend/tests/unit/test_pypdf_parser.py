"""Unit tests for the strict, bounded pypdf infrastructure adapter."""

from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf import __version__ as pypdf_version

from investment_research.infrastructure.parsing.pypdf import PypdfDocumentParser
from investment_research.modules.processing.application import (
    M0_0_PROCESSING_PROFILE,
    M1_PROCESSING_PROFILE,
    ProcessingProfile,
)
from investment_research.modules.processing.contracts import ParserLimits
from investment_research.modules.processing.ports import (
    DocumentParser,
    DocumentParserError,
    ParserLimitExceededError,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PDF = (
    REPOSITORY_ROOT / "fixtures/m0_0/documents/northstar-grid-analytics-fy2025-earnings-brief.pdf"
)
EXPECTED_PAGE_TWO_TEXT = (
    "FY2025 revenue was $184.2 million, up 22.0% from $151.0 million in FY2024."
)
GENEROUS_LIMITS = ParserLimits(
    max_pages=10,
    max_extracted_characters=100_000,
)


def encrypted_pdf() -> BytesIO:
    """Build one in-memory encrypted PDF with a synthetic test-only password."""

    content = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt("synthetic-test-password", algorithm="RC4-40")
    writer.write(content)
    content.seek(0)
    return content


@pytest.mark.parametrize("profile", [M0_0_PROCESSING_PROFILE, M1_PROCESSING_PROFILE])
def test_pypdf_adapter_matches_document_parser_protocol(profile: ProcessingProfile) -> None:
    parser: DocumentParser = PypdfDocumentParser()

    assert parser.parser_version == f"pypdf-{pypdf_version}"
    assert parser.parser_version == profile.parser_version


def test_parse_preserves_fixture_pages_and_expected_page_text() -> None:
    parser = PypdfDocumentParser()

    with FIXTURE_PDF.open("rb") as content:
        result = parser.parse(content, limits=GENEROUS_LIMITS)

        assert content.closed is False

    assert result.page_count == 4
    assert tuple(page.page_number for page in result.pages) == (1, 2, 3, 4)
    assert EXPECTED_PAGE_TWO_TEXT in result.pages[1].text
    assert result.extracted_character_count == sum(len(page.text) for page in result.pages)


def test_parse_rejects_pdf_above_page_limit() -> None:
    parser = PypdfDocumentParser()

    with (
        FIXTURE_PDF.open("rb") as content,
        pytest.raises(ParserLimitExceededError, match="page limit"),
    ):
        parser.parse(
            content,
            limits=ParserLimits(max_pages=3, max_extracted_characters=100_000),
        )


def test_parse_stops_when_extracted_text_exceeds_character_limit() -> None:
    parser = PypdfDocumentParser()

    with (
        FIXTURE_PDF.open("rb") as content,
        pytest.raises(ParserLimitExceededError, match="extracted-character limit"),
    ):
        parser.parse(
            content,
            limits=ParserLimits(max_pages=10, max_extracted_characters=1),
        )


def test_parse_rejects_encrypted_pdf_without_decryption_attempt() -> None:
    parser = PypdfDocumentParser()
    content = encrypted_pdf()

    with pytest.raises(DocumentParserError, match="encrypted PDFs are not supported"):
        parser.parse(content, limits=GENEROUS_LIMITS)

    assert content.closed is False


def test_parse_translates_malformed_pdf_failure() -> None:
    parser = PypdfDocumentParser()
    content = BytesIO(b"%PDF-1.7\nmalformed synthetic content")

    with pytest.raises(DocumentParserError, match="could not be parsed safely") as error:
        parser.parse(content, limits=GENEROUS_LIMITS)

    assert error.value.__cause__ is not None
    assert content.closed is False
