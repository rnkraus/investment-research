"""Typed application contracts for queued and bounded document processing."""

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investment_research.modules.processing.domain import ConfigurationVersion

MAX_CONFIGURABLE_PDF_PAGES = 1_000
MAX_CONFIGURABLE_EXTRACTED_CHARACTERS = 10_000_000
PROCESSING_JOB_SCHEMA_VERSION: Literal["processing-job.v1"] = "processing-job.v1"
PARSER_PROTOCOL_SCHEMA_VERSION: Literal["parser-protocol.v1"] = "parser-protocol.v1"
PDF_PARSER_CONFIG_VERSION_V1 = "m0.0-pdf-bounds-v1"
PARSER_RESPONSE_JSON_OVERHEAD_BYTES = 64 * 1024
MAX_JSON_BYTES_PER_EXTRACTED_CHARACTER = 6
MAX_JSON_BYTES_PER_PAGE = 128

type ParserFailureCode = Literal[
    "invalid_request",
    "request_too_large",
    "parse_failed",
    "limit_exceeded",
    "response_too_large",
    "internal_error",
]


class ImmutableProcessingContract(BaseModel):
    """Reject unknown fields and prevent mutation after validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")


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
