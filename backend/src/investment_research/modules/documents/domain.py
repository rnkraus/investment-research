"""Domain values and invariants for versioned archive documents."""

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator

DocumentType = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
    ),
]
DocumentTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
]
LanguageCode = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$"),
]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
StorageKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]


class DocumentLifecycleState(StrEnum):
    """Document states implemented by the M0.0 lifecycle subset."""

    ACTIVE = "ACTIVE"


class OriginalMediaType(StrEnum):
    """Original media types accepted by the M0.0 proof."""

    PDF = "application/pdf"


class DocumentScopeError(ValueError):
    """Raised when a document write crosses the trusted workspace boundary."""


class DocumentRelationshipError(ValueError):
    """Raised when a capture contains inconsistent document relationships."""


class ImmutableDocumentModel(BaseModel):
    """Shared validation policy for immutable document domain entities."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Document(ImmutableDocumentModel):
    """Logical archive item and owner of the current version pointer."""

    id: UUID
    workspace_id: UUID
    document_type: DocumentType
    lifecycle_state: DocumentLifecycleState
    current_version_id: UUID
    created_at: AwareDatetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        """Normalize the archive creation time to UTC."""

        return value.astimezone(UTC)


class DocumentVersion(ImmutableDocumentModel):
    """Immutable captured content edition and its source-time metadata."""

    id: UUID
    workspace_id: UUID
    document_id: UUID
    version_number: int = Field(ge=1)
    title: DocumentTitle
    language: LanguageCode
    content_sha256: Sha256Hex
    published_at: AwareDatetime | None = None
    available_at: AwareDatetime | None = None
    captured_at: AwareDatetime

    @field_validator("published_at", "available_at", "captured_at")
    @classmethod
    def normalize_version_times(cls, value: datetime | None) -> datetime | None:
        """Normalize each known version timestamp to UTC without inventing missing times."""

        return None if value is None else value.astimezone(UTC)


class OriginalArtifact(ImmutableDocumentModel):
    """Immutable identity and storage metadata for one captured original."""

    id: UUID
    workspace_id: UUID
    document_version_id: UUID
    storage_key: StorageKey
    media_type: OriginalMediaType
    byte_size: int = Field(gt=0)
    page_count: int = Field(gt=0)
    checksum_sha256: Sha256Hex
    captured_at: AwareDatetime

    @field_validator("storage_key")
    @classmethod
    def require_safe_storage_key(cls, value: str) -> str:
        """Reject absolute, parent-traversing, or platform-ambiguous object keys."""

        if value.startswith("/") or "\\" in value or ".." in PurePosixPath(value).parts:
            raise ValueError("storage_key must be a safe relative POSIX key")
        return value

    @field_validator("captured_at")
    @classmethod
    def normalize_captured_at(cls, value: datetime) -> datetime:
        """Normalize the artifact capture time to UTC."""

        return value.astimezone(UTC)
