"""Typed application contracts for queued and bounded document processing."""

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_CONFIGURABLE_PDF_PAGES = 1_000
MAX_CONFIGURABLE_EXTRACTED_CHARACTERS = 10_000_000
PROCESSING_JOB_SCHEMA_VERSION: Literal["processing-job.v1"] = "processing-job.v1"


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
