"""Deterministic media-aware parsing for the M1 isolated parser process."""

from re import compile as compile_pattern
from typing import BinaryIO

from investment_research.infrastructure.parsing.pypdf import PypdfDocumentParser
from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.processing.contracts import (
    M1_MAX_BLOCK_CHARACTERS,
    ParsedBlockV2,
    ParsedPdfDocument,
    ParsedTextDocument,
    ParserLimits,
    ParserMediaType,
)
from investment_research.modules.processing.domain import (
    ConfigurationVersion,
    StructureBlockType,
)
from investment_research.modules.processing.ports import (
    DocumentParserError,
    ParserLimitExceededError,
)

BLANK_LINE_PATTERN = compile_pattern(r"\n[^\S\n]*\n(?:[^\S\n]*\n)*")


class MediaAwareDocumentParser:
    """Parse PDF or canonical UTF-8 text into exact source-relative blocks."""

    def __init__(self, pdf_parser: PypdfDocumentParser | None = None) -> None:
        self._pdf_parser = pdf_parser or PypdfDocumentParser()

    @property
    def parser_version(self) -> ConfigurationVersion:
        """Reuse the exact PDF dependency version for reproducibility."""

        return self._pdf_parser.parser_version

    def parse(
        self,
        content: BinaryIO,
        *,
        limits: ParserLimits,
        media_type: ParserMediaType = "application/pdf",
    ) -> ParsedPdfDocument | ParsedTextDocument:
        """Dispatch one caller-owned stream by its trusted canonical media type."""

        if media_type == "application/pdf":
            return self._parse_pdf(content, limits=limits)
        if media_type == "text/plain":
            return self._parse_text(content, limits=limits)
        raise DocumentParserError("original media type is unsupported")

    def _parse_pdf(
        self,
        content: BinaryIO,
        *,
        limits: ParserLimits,
    ) -> ParsedPdfDocument:
        """Split exact extracted page text while preserving physical page numbers."""

        document = self._pdf_parser.parse(content, limits=limits)
        blocks: list[ParsedBlockV2] = []
        for page in document.pages:
            blocks.extend(
                _split_visible_source(
                    page.text,
                    first_ordinal=len(blocks),
                    source_locator_kind=SourceLocatorKind.PDF_PAGE,
                    page_number=page.page_number,
                )
            )
        if not blocks:
            raise DocumentParserError("PDF contains no visible text")
        return ParsedPdfDocument(
            physical_page_count=document.page_count,
            blocks=tuple(blocks),
        )

    @staticmethod
    def _parse_text(
        content: BinaryIO,
        *,
        limits: ParserLimits,
    ) -> ParsedTextDocument:
        """Decode one unchanged canonical UTF-8 original and split exact ranges."""

        raw_content = content.read()
        try:
            text = raw_content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise DocumentParserError("plain-text original is not valid UTF-8") from error
        if "\r" in text:
            raise DocumentParserError("plain-text original has non-canonical newlines")
        if "\x00" in text:
            raise DocumentParserError("plain-text original contains NUL")
        if len(text) > limits.max_extracted_characters:
            raise ParserLimitExceededError(
                "plain-text original exceeds the extracted-character limit"
            )

        blocks = _split_visible_source(
            text,
            first_ordinal=0,
            source_locator_kind=SourceLocatorKind.TEXT_PASSAGE,
            page_number=None,
        )
        if not blocks:
            raise DocumentParserError("plain-text original contains no visible text")
        return ParsedTextDocument(blocks=tuple(blocks))


def _split_visible_source(
    source: str,
    *,
    first_ordinal: int,
    source_locator_kind: SourceLocatorKind,
    page_number: int | None,
) -> list[ParsedBlockV2]:
    """Split one source at blank lines, then at bounded whitespace-aware ranges."""

    paragraph_ranges: list[tuple[int, int]] = []
    cursor = 0
    for separator in BLANK_LINE_PATTERN.finditer(source):
        visible_range = _visible_range(source, cursor, separator.start())
        if visible_range is not None:
            paragraph_ranges.append(visible_range)
        cursor = separator.end()
    visible_range = _visible_range(source, cursor, len(source))
    if visible_range is not None:
        paragraph_ranges.append(visible_range)

    blocks: list[ParsedBlockV2] = []
    for paragraph_start, paragraph_end in paragraph_ranges:
        for block_start, block_end in _bounded_ranges(
            source,
            paragraph_start,
            paragraph_end,
        ):
            blocks.append(
                ParsedBlockV2(
                    ordinal=first_ordinal + len(blocks),
                    source_locator_kind=source_locator_kind,
                    page_number=page_number,
                    block_type=StructureBlockType.PARAGRAPH,
                    text=source[block_start:block_end],
                    start_character=block_start,
                    end_character=block_end,
                )
            )
    return blocks


def _visible_range(source: str, start: int, end: int) -> tuple[int, int] | None:
    """Trim only boundary whitespace and retain absolute source coordinates."""

    while start < end and source[start].isspace():
        start += 1
    while end > start and source[end - 1].isspace():
        end -= 1
    return None if start == end else (start, end)


def _bounded_ranges(source: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split oversized visible text at the last available bounded whitespace."""

    ranges: list[tuple[int, int]] = []
    while end - start > M1_MAX_BLOCK_CHARACTERS:
        hard_end = start + M1_MAX_BLOCK_CHARACTERS
        whitespace_index = hard_end
        while whitespace_index > start and not source[whitespace_index].isspace():
            whitespace_index -= 1

        block_end = hard_end if whitespace_index == start else whitespace_index
        visible_range = _visible_range(source, start, block_end)
        if visible_range is not None:
            ranges.append(visible_range)

        start = block_end
        while start < end and source[start].isspace():
            start += 1
    visible_range = _visible_range(source, start, end)
    if visible_range is not None:
        ranges.append(visible_range)
    return ranges
