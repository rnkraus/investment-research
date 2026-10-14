"""Application-owned persistence and storage contracts for archive documents."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import BinaryIO, Protocol
from uuid import UUID

from investment_research.modules.delivery.ports import OutboxRepository
from investment_research.modules.documents.contracts import DocumentListPage, DocumentListQuery
from investment_research.modules.documents.domain import (
    Document,
    DocumentVersion,
    OriginalArtifact,
    Sha256Hex,
    StorageKey,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.ports import ProcessingRepository


class ObjectStorageError(Exception):
    """Base error exposed by application-owned object-storage adapters."""


class ObjectAlreadyExistsError(ObjectStorageError):
    """Raised when immutable storage already contains the requested key."""


class ObjectNotFoundError(ObjectStorageError):
    """Hide whether an object is missing, invalid, or outside the active workspace."""


class ObjectStorageScopeError(ObjectStorageError):
    """Raised before a write whose key is outside the active workspace."""


class CaptureCommitUncertainError(Exception):
    """A commit may have succeeded; never compensate its possibly canonical original."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Verified metadata computed while an immutable object is stored."""

    storage_key: StorageKey
    byte_size: int
    checksum_sha256: Sha256Hex


class BinaryReadable(Protocol):
    """Minimum binary stream behavior required by immutable storage."""

    def read(self, size: int = -1, /) -> bytes:
        """Read at most size bytes, or all remaining bytes when size is negative."""


class ObjectStorage(Protocol):
    """Store and open immutable objects inside one trusted workspace."""

    def store_immutable(
        self,
        context: WorkspaceContext,
        storage_key: StorageKey,
        content: BinaryReadable,
    ) -> StoredObject:
        """Atomically store new content and reject an existing or out-of-scope key."""

    def open(
        self,
        context: WorkspaceContext,
        storage_key: StorageKey,
    ) -> AbstractContextManager[BinaryIO]:
        """Open an in-scope object or return the same error for all unavailable keys."""

    def discard_uncommitted(
        self,
        context: WorkspaceContext,
        storage_key: StorageKey,
    ) -> None:
        """Remove a newly stored object after its canonical transaction fails."""


class DocumentRepository(Protocol):
    """Persist and retrieve document aggregates within one trusted workspace."""

    def list_current_documents(
        self,
        context: WorkspaceContext,
        query: DocumentListQuery,
    ) -> DocumentListPage:
        """Return a bounded metadata page for active current originals in scope."""

    def add_capture(
        self,
        context: WorkspaceContext,
        document: Document,
        version: DocumentVersion,
        original: OriginalArtifact,
    ) -> None:
        """Stage one logical document, its current version, and immutable original."""

    def get_document_by_id(
        self,
        context: WorkspaceContext,
        document_id: UUID,
    ) -> Document | None:
        """Return an active document without disclosing another workspace's records."""

    def get_current_version_by_id(
        self,
        context: WorkspaceContext,
        document_id: UUID,
        version_id: UUID,
    ) -> DocumentVersion | None:
        """Return a version only when it is current, active, and in scope."""

    def get_original_artifact(
        self,
        context: WorkspaceContext,
        document_id: UUID,
        version_id: UUID,
    ) -> OriginalArtifact | None:
        """Return original metadata only for an eligible current document version."""

    def get_original_artifact_by_version_id(
        self,
        context: WorkspaceContext,
        version_id: UUID,
    ) -> OriginalArtifact | None:
        """Resolve an eligible current original for internal processing."""


class CaptureUnitOfWork(Protocol):
    """Repositories participating in one atomic document-capture transaction."""

    @property
    def documents(self) -> DocumentRepository:
        """Return the repository that stages document capture metadata."""

    @property
    def processing(self) -> ProcessingRepository:
        """Return the repository that stages the associated processing intent."""

    @property
    def outbox(self) -> OutboxRepository:
        """Stage durable publication intent in the same capture transaction."""


class DocumentReadUnitOfWork(Protocol):
    """Document repository scoped to one non-writing application operation."""

    @property
    def documents(self) -> DocumentRepository:
        """Return the repository used to resolve eligible document records."""


type CaptureUnitOfWorkFactory = Callable[[], AbstractContextManager[CaptureUnitOfWork]]
type DocumentReadUnitOfWorkFactory = Callable[
    [],
    AbstractContextManager[DocumentReadUnitOfWork],
]
