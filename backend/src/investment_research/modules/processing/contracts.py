"""Typed application contracts for queued and bounded document processing."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.processing.domain import ConfigurationVersion, StructureBlockType

MAX_CONFIGURABLE_PDF_PAGES = 1_000
MAX_CONFIGURABLE_EXTRACTED_CHARACTERS = 10_000_000
PROCESSING_JOB_SCHEMA_VERSION: Literal["processing-job.v1"] = "processing-job.v1"
PARSER_PROTOCOL_SCHEMA_VERSION: Literal["parser-protocol.v1"] = "parser-protocol.v1"
PARSER_PROTOCOL_V2_SCHEMA_VERSION: Literal["parser-protocol.v2"] = "parser-protocol.v2"
PDF_PARSER_CONFIG_VERSION_V1 = "m0.0-pdf-bounds-v1"
M1_PARSER_CONFIG_VERSION = "m1-media-aware-blocks-v1"
M1_MAX_BLOCK_CHARACTERS: Literal[4_000] = 4_000
PARSER_RESPONSE_JSON_OVERHEAD_BYTES = 64 * 1024
MAX_JSON_BYTES_PER_EXTRACTED_CHARACTER = 6
MAX_JSON_BYTES_PER_PAGE = 128

type ParserFailureCode = Literal[
    "invalid_request",
    "request_too_large",
    "parse_failed",
    "limit_exceeded",
    "response_too_large",
    "timeout",
    "internal_error",
]

type ParserMediaType = Literal["application/pdf", "text/plain"]


class ImmutableProcessingContract(BaseModel):
    """Reject unknown fields and prevent mutation after validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ProcessingStatusQuery(ImmutableProcessingContract):
    """Bound status reads for versions supplied by a trusted application caller."""

    document_version_ids: tuple[UUID, ...] = Field(max_length=100)


def processing_job_idempotency_key(
    workspace_id: UUID,
    processing_run_id: UUID,
) -> str:
    """Derive the stable identity shared by redeliveries of one processing run."""

    return f"{PROCESSING_JOB_SCHEMA_VERSION}:{workspace_id}:{processing_run_id}"


class ProcessingJobV1(ImmutableProcessingContract):
    """Small versioned message that identifies one canonical processing intent."""

    schema_version: Literal["processing-job.v1"] = PROCESSING_JOB_SCHEMA_VERSION
    workspace_id: UUID
    processing_run_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=128)

    @classmethod
    def create(
        cls,
        *,
        workspace_id: UUID,
        processing_run_id: UUID,
    ) -> Self:
        """Create a job with its application-owned deterministic identity."""

        return cls(
            workspace_id=workspace_id,
            processing_run_id=processing_run_id,
            idempotency_key=processing_job_idempotency_key(
                workspace_id,
                processing_run_id,
            ),
        )

    @model_validator(mode="after")
    def require_matching_idempotency_key(self) -> Self:
        """Reject forged or stale identities before a job reaches application code."""

        expected_key = processing_job_idempotency_key(
            self.workspace_id,
            self.processing_run_id,
        )
        if self.idempotency_key != expected_key:
            raise ValueError("idempotency_key does not match the processing job identity")
        return self


class ParserLimits(ImmutableProcessingContract):
    """Fail-closed limits enforced while extracting text from one PDF."""

    max_pages: int = Field(ge=1, le=MAX_CONFIGURABLE_PDF_PAGES)
    max_extracted_characters: int = Field(
        ge=1,
        le=MAX_CONFIGURABLE_EXTRACTED_CHARACTERS,
    )


def parser_response_byte_limit(limits: ParserLimits) -> int:
    """Bound JSON output for the requested text and page ceilings."""

    return (
        PARSER_RESPONSE_JSON_OVERHEAD_BYTES
        + limits.max_extracted_characters * MAX_JSON_BYTES_PER_EXTRACTED_CHARACTER
        + limits.max_pages * MAX_JSON_BYTES_PER_PAGE
    )


class ParserRequestV1(ImmutableProcessingContract):
    """Non-secret metadata accompanying one bounded raw PDF request."""

    schema_version: Literal["parser-protocol.v1"] = PARSER_PROTOCOL_SCHEMA_VERSION
    parser_version: ConfigurationVersion
    parser_config_version: ConfigurationVersion
    limits: ParserLimits


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


class ParserResponseV1(ImmutableProcessingContract):
    """Successful bounded parser response returned to the trusted worker."""

    schema_version: Literal["parser-protocol.v1"] = PARSER_PROTOCOL_SCHEMA_VERSION
    parser_version: ConfigurationVersion
    parser_config_version: ConfigurationVersion
    document: ParsedDocument


class ParserFailureV1(ImmutableProcessingContract):
    """Safe parser failure without document text or implementation details."""

    schema_version: Literal["parser-protocol.v1"] = PARSER_PROTOCOL_SCHEMA_VERSION
    code: ParserFailureCode


class ParserRequestV2(ImmutableProcessingContract):
    """Media-aware limits accompanying one raw immutable original."""

    schema_version: Literal["parser-protocol.v2"] = PARSER_PROTOCOL_V2_SCHEMA_VERSION
    media_type: ParserMediaType
    parser_version: ConfigurationVersion
    parser_config_version: ConfigurationVersion
    max_input_bytes: int = Field(ge=1)
    max_extracted_characters: int = Field(
        ge=1,
        le=MAX_CONFIGURABLE_EXTRACTED_CHARACTERS,
    )
    max_pdf_pages: int | None = Field(default=None, ge=1, le=MAX_CONFIGURABLE_PDF_PAGES)
    max_block_characters: Literal[4_000] = M1_MAX_BLOCK_CHARACTERS

    @model_validator(mode="after")
    def require_media_specific_limits(self) -> Self:
        """Require the physical-page ceiling only for PDF originals."""

        if self.media_type == "application/pdf" and self.max_pdf_pages is None:
            raise ValueError("PDF parser request requires max_pdf_pages")
        if self.media_type == "text/plain" and self.max_pdf_pages is not None:
            raise ValueError("plain-text parser request must not define max_pdf_pages")
        return self


class ParsedBlockV2(ImmutableProcessingContract):
    """Exact visible source slice emitted by the M1 parser."""

    ordinal: int = Field(ge=0)
    source_locator_kind: SourceLocatorKind
    page_number: int | None = Field(default=None, ge=1)
    block_type: StructureBlockType
    text: str = Field(min_length=1)
    start_character: int = Field(ge=0)
    end_character: int = Field(gt=0)

    @model_validator(mode="after")
    def require_exact_locator_shape(self) -> Self:
        """Reject invisible text, mismatched ranges, and mixed locator fields."""

        if not self.text.strip():
            raise ValueError("parsed block must contain visible text")
        if self.end_character <= self.start_character:
            raise ValueError("parsed block range must be increasing")
        if self.end_character - self.start_character != len(self.text):
            raise ValueError("parsed block range must match its text length")
        if self.source_locator_kind is SourceLocatorKind.PDF_PAGE:
            if self.page_number is None:
                raise ValueError("PDF_PAGE block requires page_number")
        elif self.page_number is not None:
            raise ValueError("TEXT_PASSAGE block must not define page_number")
        return self


class ParsedPdfDocument(ImmutableProcessingContract):
    """Ordered source blocks anchored to one-based physical PDF pages."""

    kind: Literal["PDF"] = "PDF"
    physical_page_count: int = Field(ge=1)
    blocks: tuple[ParsedBlockV2, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_pdf_blocks(self) -> Self:
        """Require contiguous ordinals and valid physical-page references."""

        _require_contiguous_block_ordinals(self.blocks)
        for block in self.blocks:
            if block.source_locator_kind is not SourceLocatorKind.PDF_PAGE:
                raise ValueError("PDF document contains a non-PDF block")
            if block.page_number is None or block.page_number > self.physical_page_count:
                raise ValueError("PDF block references an invalid physical page")
        return self

    @property
    def extracted_character_count(self) -> int:
        """Return visible characters emitted across all PDF blocks."""

        return sum(len(block.text) for block in self.blocks)


class ParsedTextDocument(ImmutableProcessingContract):
    """Ordered source blocks anchored to one canonical pasted-text original."""

    kind: Literal["PASTED_TEXT"] = "PASTED_TEXT"
    blocks: tuple[ParsedBlockV2, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_text_blocks(self) -> Self:
        """Require contiguous ordinals and text-only locators."""

        _require_contiguous_block_ordinals(self.blocks)
        if any(
            block.source_locator_kind is not SourceLocatorKind.TEXT_PASSAGE for block in self.blocks
        ):
            raise ValueError("pasted-text document contains a non-text block")
        return self

    @property
    def extracted_character_count(self) -> int:
        """Return visible characters emitted across all text blocks."""

        return sum(len(block.text) for block in self.blocks)


type ParsedDocumentV2 = Annotated[
    ParsedPdfDocument | ParsedTextDocument,
    Field(discriminator="kind"),
]


class ParserResponseV2(ImmutableProcessingContract):
    """Successful media-aware parser response returned to the trusted worker."""

    schema_version: Literal["parser-protocol.v2"] = PARSER_PROTOCOL_V2_SCHEMA_VERSION
    parser_version: ConfigurationVersion
    parser_config_version: ConfigurationVersion
    document: ParsedDocumentV2


class ParserFailureV2(ImmutableProcessingContract):
    """Safe v2 parser failure without original text or internal details."""

    schema_version: Literal["parser-protocol.v2"] = PARSER_PROTOCOL_V2_SCHEMA_VERSION
    code: ParserFailureCode


def parser_response_v2_byte_limit(request: ParserRequestV2) -> int:
    """Bound v2 JSON output using the caller-owned extraction ceiling."""

    return (
        PARSER_RESPONSE_JSON_OVERHEAD_BYTES
        + request.max_extracted_characters * MAX_JSON_BYTES_PER_EXTRACTED_CHARACTER
        + request.max_extracted_characters * MAX_JSON_BYTES_PER_PAGE
    )


def _require_contiguous_block_ordinals(blocks: tuple[ParsedBlockV2, ...]) -> None:
    """Keep serialized order identical to zero-based canonical block order."""

    ordinals = tuple(block.ordinal for block in blocks)
    expected = tuple(range(len(blocks)))
    if ordinals != expected:
        raise ValueError("parsed block ordinals must be contiguous from zero")
