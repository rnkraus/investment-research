"""Application-owned contracts for irreversible, workspace-scoped document purges."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from investment_research.modules.documents.deletion_contracts import (
    DeleteDocumentsCommand,
    DocumentDeletionReceipt,
    DocumentPurgeJobV1,
)
from investment_research.modules.documents.domain import StorageKey
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.contracts import DenseVectorId

DENSE_VECTOR_PURGE_BATCH_LIMIT = 100


class DocumentDeletionUnavailableError(ValueError):
    """Hide whether a requested target is missing or belongs to another workspace."""


class DocumentDeletionBusyError(ValueError):
    """Refuse the whole batch while any target still has unfinished processing."""


class DocumentPurgePendingError(RuntimeError):
    """Keep durable intent pending when external absence cannot yet be confirmed."""


class DocumentPurgeDispatchError(RuntimeError):
    """Keep committed deletion intent when the broker cannot accept its delivery."""


class DocumentPurgeDispatcher(Protocol):
    def dispatch(self, job: DocumentPurgeJobV1) -> None:
        """Publish an ID-only job after canonical admission has committed."""


class DocumentPurgeJobExecutor(Protocol):
    def execute_purge(self, job: DocumentPurgeJobV1) -> DocumentDeletionReceipt:
        """Resolve server-controlled workspace scope before invoking permanent cleanup."""


@dataclass(frozen=True, slots=True)
class DocumentPurgeManifest:
    """Exact canonical cleanup identities; never accept these from an HTTP client."""

    workspace_id: UUID
    document_id: UUID
    original_keys: tuple[StorageKey, ...]
    vector_ids: tuple[DenseVectorId, ...]


class OriginalPurger(Protocol):
    """Separate permanent cleanup from compensation of an uncommitted capture."""

    def delete_original(self, context: WorkspaceContext, storage_key: StorageKey) -> None:
        """Delete one exact scoped key and confirm absence, or raise a storage error."""


class DenseVectorPurger(Protocol):
    """Remove only explicit canonical vector IDs in the trusted namespace."""

    def delete_vectors(
        self, context: WorkspaceContext, vector_ids: tuple[DenseVectorId, ...]
    ) -> None:
        """Confirm absence of one bounded batch or raise a safe provider error."""


class DocumentDeletionRepository(Protocol):
    """One caller-owned transaction across the source and dependent-answer graph."""

    def request(
        self, context: WorkspaceContext, command: DeleteDocumentsCommand, requested_at: datetime
    ) -> tuple[DocumentDeletionReceipt, ...]:
        """Atomically admit every target or none; repeated targets return their receipts."""

    def get_receipt(
        self, context: WorkspaceContext, document_id: UUID
    ) -> DocumentDeletionReceipt | None:
        """Return a scoped receipt without disclosing inaccessible targets."""

    def load_manifest(self, context: WorkspaceContext, document_id: UUID) -> DocumentPurgeManifest:
        """Resolve all versions and runs of one pending document from canonical state."""

    def complete(
        self, context: WorkspaceContext, document_id: UUID, completed_at: datetime
    ) -> DocumentDeletionReceipt:
        """Purge the canonical graph and finalize its receipt in the same transaction."""


class DocumentDeletionUnitOfWork(Protocol):
    @property
    def deletions(self) -> DocumentDeletionRepository:
        """Return deletion persistence bound to this short transaction."""


type DocumentDeletionUnitOfWorkFactory = Callable[
    [], AbstractContextManager[DocumentDeletionUnitOfWork]
]
