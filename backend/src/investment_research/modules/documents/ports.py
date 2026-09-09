"""Application-owned persistence and storage contracts for archive documents."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import BinaryIO, Protocol
from uuid import UUID

from investment_research.modules.documents.domain import (
    Document,
    DocumentVersion,
    OriginalArtifact,
    Sha256Hex,
    StorageKey,
)
from investment_research.modules.identity.domain import WorkspaceContext


class ObjectStorageError(Exception):
    """Base error exposed by application-owned object-storage adapters."""


class ObjectAlreadyExistsError(ObjectStorageError):
    """Raised when immutable storage already contains the requested key."""


class ObjectNotFoundError(ObjectStorageError):
    """Hide whether an object is missing, invalid, or outside the active workspace."""


class ObjectStorageScopeError(ObjectStorageError):
    """Raised before a write whose key is outside the active workspace."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Verified metadata computed while an immutable object is stored."""

    storage_key: StorageKey
    byte_size: int
    checksum_sha256: Sha256Hex


class ObjectStorage(Protocol):
    """Store and open immutable objects inside one trusted workspace."""

    def store_immutable(
        self,
        context: WorkspaceContext,
        storage_key: StorageKey,
        content: BinaryIO,
    ) -> StoredObject:
        """Atomically store new content and reject an existing or out-of-scope key."""

    def open(
        self,
        context: WorkspaceContext,
        storage_key: StorageKey,
    ) -> AbstractContextManager[BinaryIO]:
        """Open an in-scope object or return the same error for all unavailable keys."""


class DocumentRepository(Protocol):
    """Persist and retrieve document aggregates within one trusted workspace."""

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
