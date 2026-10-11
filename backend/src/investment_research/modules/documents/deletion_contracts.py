"""Bounded, workspace-neutral input and content-free permanent-deletion receipts."""

from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

DOCUMENT_DELETION_LIMIT = 100


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
