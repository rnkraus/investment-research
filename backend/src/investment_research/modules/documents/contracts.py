"""Typed application contracts for capturing archive documents."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    StringConstraints,
    field_validator,
)

from investment_research.modules.documents.domain import DocumentTitle, LanguageCode
from investment_research.modules.processing.domain import ProcessingState

OriginalFilename = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=255,
        pattern=r"^[^/\\\x00]+$",
    ),
]
SubmittedMediaType = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=127),
]


class ImmutableContract(BaseModel):
    """Shared strict policy for application-layer capture contracts."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class CaptureDocumentInput(ImmutableContract):
    """Validated metadata supplied with one untrusted original stream."""

    filename: OriginalFilename
    media_type: SubmittedMediaType
    title: DocumentTitle
    language: LanguageCode = "en"
    published_at: AwareDatetime | None = None
    available_at: AwareDatetime | None = None

    @field_validator("published_at", "available_at")
    @classmethod
    def normalize_source_times(cls, value: datetime | None) -> datetime | None:
        """Normalize known source times without inventing missing metadata."""

        return None if value is None else value.astimezone(UTC)


class CaptureDocumentResult(ImmutableContract):
    """Stable identifiers returned after storage and canonical state commit."""

    document_id: UUID
    document_version_id: UUID
    original_artifact_id: UUID
    processing_run_id: UUID
    state: ProcessingState
