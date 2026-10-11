"""Bounded, workspace-neutral input and content-free permanent-deletion receipts."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

DOCUMENT_DELETION_LIMIT = 100
DOCUMENT_PURGE_JOB_SCHEMA_VERSION: Literal["document-purge-job.v1"] = "document-purge-job.v1"


class DocumentPurgeJobV1(BaseModel):
    """Identify a recorded pending purge, never carry cleanup targets or authorization."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    schema_version: Literal["document-purge-job.v1"] = DOCUMENT_PURGE_JOB_SCHEMA_VERSION
    workspace_id: UUID
    document_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=128)

    @classmethod
    def create(cls, *, workspace_id: UUID, document_id: UUID) -> Self:
        return cls(
            workspace_id=workspace_id,
            document_id=document_id,
            idempotency_key=f"{DOCUMENT_PURGE_JOB_SCHEMA_VERSION}:{workspace_id}:{document_id}",
        )

    @model_validator(mode="after")
    def require_matching_identity(self) -> Self:
        expected = f"{DOCUMENT_PURGE_JOB_SCHEMA_VERSION}:{self.workspace_id}:{self.document_id}"
        if self.idempotency_key != expected:
            raise ValueError("idempotency_key does not match the purge job identity")
        return self


class DocumentDeletionState(StrEnum):
    """A pending purge is already irreversible; completion confirms store cleanup."""

    PENDING = "PENDING"
    COMPLETED = "COMPLETED"


class DeleteDocumentsCommand(BaseModel):
    """Explicit target identities; an empty list must never mean delete everything."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    document_ids: tuple[UUID, ...] = Field(min_length=1, max_length=DOCUMENT_DELETION_LIMIT)

    @model_validator(mode="after")
    def require_unique_targets(self) -> Self:
        if len(self.document_ids) != len(set(self.document_ids)):
            raise ValueError("document deletion requires unique document IDs")
        return self


class DocumentDeletionReceipt(BaseModel):
    """Minimal durable audit state without source titles, text, keys, or answers."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    workspace_id: UUID
    document_id: UUID
    state: DocumentDeletionState
    requested_at: AwareDatetime
    completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def require_coherent_completion(self) -> Self:
        if (self.state is DocumentDeletionState.COMPLETED) != (self.completed_at is not None):
            raise ValueError("only completed deletion receipts require completed_at")
        if self.completed_at is not None and self.completed_at < self.requested_at:
            raise ValueError("deletion completion must not precede the request")
        return self


@dataclass(frozen=True, slots=True)
class DocumentDeletionSubmission:
    """Committed receipt snapshots and failures of this publication attempt only."""

    receipts: tuple[DocumentDeletionReceipt, ...]
    dispatch_pending_document_ids: tuple[UUID, ...]
