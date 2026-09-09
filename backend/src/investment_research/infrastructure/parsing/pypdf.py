"""Strict, bounded pypdf adapter for text-based PDF documents."""

from typing import BinaryIO

from pypdf import PdfReader
from pypdf import __version__ as pypdf_version
from pypdf.errors import PyPdfError

from investment_research.modules.processing.contracts import (
    ParsedDocument,
    ParsedPage,
    ParserLimits,
)
from investment_research.modules.processing.domain import ConfigurationVersion
from investment_research.modules.processing.ports import (
    DocumentParserError,
    ParserLimitExceededError,
)


class PypdfDocumentParser:
    """Extract exact page text while enforcing application-owned limits."""

    @property
    def parser_version(self) -> ConfigurationVersion:
        """Identify the exact parser dependency used for reproducibility."""

        return f"pypdf-{pypdf_version}"

    def parse(self, content: BinaryIO, *, limits: ParserLimits) -> ParsedDocument:
        """Parse a caller-owned PDF stream without closing it."""

        try:
            reader = PdfReader(content, strict=True)
            if reader.is_encrypted:
                raise DocumentParserError("encrypted PDFs are not supported")

            page_count = len(reader.pages)
            if page_count == 0:
                raise DocumentParserError("PDF contains no pages")
            if page_count > limits.max_pages:
                raise ParserLimitExceededError("PDF exceeds the configured page limit")

            pages: list[ParsedPage] = []
            extracted_character_count = 0
            for page_number, page in enumerate(reader.pages, start=1):
                text = page.extract_text(extraction_mode="plain")
                extracted_character_count += len(text)
                if extracted_character_count > limits.max_extracted_characters:
                    raise ParserLimitExceededError(
                        "PDF exceeds the configured extracted-character limit"
                    )
                pages.append(ParsedPage(page_number=page_number, text=text))
        except (EOFError, PyPdfError) as error:
            raise DocumentParserError("PDF could not be parsed safely") from error

        return ParsedDocument(pages=tuple(pages))
