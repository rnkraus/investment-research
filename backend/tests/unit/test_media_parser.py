"""Unit tests for deterministic M1 PDF and pasted-text block extraction."""

from io import BytesIO
from pathlib import Path

import pytest

from investment_research.infrastructure.parsing.media import MediaAwareDocumentParser
from investment_research.infrastructure.parsing.pypdf import PypdfDocumentParser
from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.processing.contracts import (
    ParsedPdfDocument,
    ParsedTextDocument,
    ParserLimits,
)
from investment_research.modules.processing.ports import (
    DocumentParserError,
    ParserLimitExceededError,
)

LIMITS = ParserLimits(max_pages=10, max_extracted_characters=20_000)
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PDF = (
    REPOSITORY_ROOT / "fixtures/m0_0/documents/northstar-grid-analytics-fy2025-earnings-brief.pdf"
)


def test_pdf_blocks_preserve_physical_pages_and_exact_page_ranges() -> None:
    pdf_bytes = FIXTURE_PDF.read_bytes()
    pages = PypdfDocumentParser().parse(BytesIO(pdf_bytes), limits=LIMITS)
    document = MediaAwareDocumentParser().parse(
        BytesIO(pdf_bytes),
        limits=LIMITS,
        media_type="application/pdf",
    )

    assert isinstance(document, ParsedPdfDocument)
    assert document.physical_page_count == 4
    assert tuple(block.ordinal for block in document.blocks) == tuple(range(len(document.blocks)))
    assert {block.page_number for block in document.blocks} == {1, 2, 3, 4}
    assert all(block.source_locator_kind is SourceLocatorKind.PDF_PAGE for block in document.blocks)
    page_text_by_number = {page.page_number: page.text for page in pages.pages}
    for block in document.blocks:
        assert block.page_number is not None
        page_text = page_text_by_number[block.page_number]
        assert page_text[block.start_character : block.end_character] == block.text


def parse_text(source: str) -> ParsedTextDocument:
    """Parse one canonical string and narrow the result for concise assertions."""

    result = MediaAwareDocumentParser().parse(
        BytesIO(source.encode("utf-8")),
        limits=LIMITS,
        media_type="text/plain",
    )
    assert isinstance(result, ParsedTextDocument)
    return result


def test_text_blocks_preserve_exact_absolute_source_ranges() -> None:
    source = "  Revenue grew.  \n\n\tMargin expanded.\n "

    document = parse_text(source)

    assert tuple(block.ordinal for block in document.blocks) == (0, 1)
    assert tuple(block.text for block in document.blocks) == (
        "Revenue grew.",
        "Margin expanded.",
    )
    for block in document.blocks:
        assert block.source_locator_kind is SourceLocatorKind.TEXT_PASSAGE
        assert block.page_number is None
        assert source[block.start_character : block.end_character] == block.text


def test_oversized_paragraph_splits_at_whitespace_without_overlap() -> None:
    source = "a" * 3_990 + " " + "b" * 20

    document = parse_text(source)

    assert tuple(len(block.text) for block in document.blocks) == (3_990, 20)
    assert document.blocks[0].end_character == 3_990
    assert document.blocks[1].start_character == 3_991


def test_oversized_unbroken_paragraph_splits_exactly_at_limit() -> None:
    source = "x" * 4_001

    document = parse_text(source)

    assert tuple(len(block.text) for block in document.blocks) == (4_000, 1)
    assert document.blocks[0].end_character == document.blocks[1].start_character


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"\xff", id="invalid-utf8"),
        pytest.param(b"first\r\nsecond", id="non-canonical-newline"),
        pytest.param(b"\x00visible", id="nul"),
        pytest.param(b" \n\t", id="no-visible-text"),
    ],
)
def test_text_parser_rejects_invalid_canonical_content(content: bytes) -> None:
    with pytest.raises(DocumentParserError):
        MediaAwareDocumentParser().parse(
            BytesIO(content),
            limits=LIMITS,
            media_type="text/plain",
        )


def test_text_parser_enforces_extracted_character_limit() -> None:
    with pytest.raises(ParserLimitExceededError):
        MediaAwareDocumentParser().parse(
            BytesIO(b"visible"),
            limits=ParserLimits(max_pages=1, max_extracted_characters=6),
            media_type="text/plain",
        )
