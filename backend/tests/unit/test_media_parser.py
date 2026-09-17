"""Unit tests for deterministic M1 PDF and pasted-text block extraction."""

from io import BytesIO

import pytest

from investment_research.infrastructure.parsing.media import MediaAwareDocumentParser
from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.processing.contracts import (
    ParsedTextDocument,
    ParserLimits,
)
from investment_research.modules.processing.ports import (
    DocumentParserError,
    ParserLimitExceededError,
)

LIMITS = ParserLimits(max_pages=10, max_extracted_characters=20_000)


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
