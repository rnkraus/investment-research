"""Typed application contracts for capturing and listing archive documents."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from investment_research.modules.documents.domain import (
    DocumentTitle,
    LanguageCode,
    OriginalMediaType,
)
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
MAX_PASTED_TEXT_CHARACTERS = 100_000
SubmittedMediaType = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=127),
]


class ImmutableContract(BaseModel):
    """Shared strict policy for application-layer capture contracts."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class CaptureMetadata(ImmutableContract):
    """Metadata shared by every supported immutable-original input."""

    title: DocumentTitle
    language: LanguageCode = "en"
    published_at: AwareDatetime | None = None
    available_at: AwareDatetime | None = None

    @field_validator("published_at", "available_at")
    @classmethod
    def normalize_source_times(cls, value: datetime | None) -> datetime | None:
        """Normalize known source times without inventing missing metadata."""

        return None if value is None else value.astimezone(UTC)


class PdfCaptureInput(CaptureMetadata):
    """Validated PDF metadata supplied with one untrusted binary stream."""

    kind: Literal["PDF"] = "PDF"
    filename: OriginalFilename
    media_type: SubmittedMediaType


class PastedTextCaptureInput(CaptureMetadata):
    """Validated English pasted text before canonical newline normalization."""

    kind: Literal["PASTED_TEXT"] = "PASTED_TEXT"
    language: Literal["en"] = "en"
    text: Annotated[str, StringConstraints(max_length=MAX_PASTED_TEXT_CHARACTERS)]


class CaptureDocumentResult(ImmutableContract):
    """Stable identifiers returned after storage and canonical state commit."""

    document_id: UUID
    document_version_id: UUID
    original_artifact_id: UUID
    processing_run_id: UUID
    state: ProcessingState


class DocumentListCursor(ImmutableContract):
    """Exclusive position in descending document creation-time and UUID order."""

    created_at: AwareDatetime
    document_id: UUID

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        """Keep the continuation position timezone-aware and normalized to UTC."""

        return value.astimezone(UTC)


class DocumentListQuery(ImmutableContract):
    """Bound one read page without accepting a client-controlled workspace."""

    page_size: int = Field(default=20, ge=1, le=100, strict=True)
    after: DocumentListCursor | None = None


class DocumentListEntry(CaptureMetadata):
    """Current original metadata safe for a workspace-owned archive listing."""

    document_id: UUID
    document_version_id: UUID
    media_type: OriginalMediaType
    byte_size: int = Field(gt=0)
    created_at: AwareDatetime
    captured_at: AwareDatetime

    @field_validator("created_at", "captured_at")
    @classmethod
    def normalize_archive_times(cls, value: datetime) -> datetime:
        """Preserve separate document-creation and version-capture timestamps."""

        return value.astimezone(UTC)


class DocumentListPage(ImmutableContract):
    """A bounded metadata page and an optional exclusive continuation position."""

    items: tuple[DocumentListEntry, ...] = Field(max_length=100)
    next_cursor: DocumentListCursor | None = None
