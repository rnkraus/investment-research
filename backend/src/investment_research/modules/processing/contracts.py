"""Typed application contracts for bounded document parsing."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_CONFIGURABLE_PDF_PAGES = 1_000
MAX_CONFIGURABLE_EXTRACTED_CHARACTERS = 10_000_000


class ImmutableProcessingContract(BaseModel):
    """Reject unknown fields and prevent mutation after validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ParserLimits(ImmutableProcessingContract):
    """Fail-closed limits enforced while extracting text from one PDF."""

    max_pages: int = Field(ge=1, le=MAX_CONFIGURABLE_PDF_PAGES)
    max_extracted_characters: int = Field(
        ge=1,
        le=MAX_CONFIGURABLE_EXTRACTED_CHARACTERS,
    )


class ParsedPage(ImmutableProcessingContract):
    """Exact text extracted from one physical PDF page."""

    page_number: int = Field(ge=1)
    text: str


class ParsedDocument(ImmutableProcessingContract):
    """Ordered page-aware output returned by an application-owned parser port."""

    pages: tuple[ParsedPage, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_contiguous_page_numbers(self) -> Self:
        """Keep tuple order identical to one-based physical PDF page order."""

        page_numbers = tuple(page.page_number for page in self.pages)
        expected_numbers = tuple(range(1, len(self.pages) + 1))
        if page_numbers != expected_numbers:
            raise ValueError("parsed pages must be contiguous and ordered from page 1")
        return self

    @property
    def page_count(self) -> int:
        """Return the physical page count represented by this result."""

        return len(self.pages)

    @property
    def extracted_character_count(self) -> int:
        """Return the total number of extracted Unicode characters."""

        return sum(len(page.text) for page in self.pages)
