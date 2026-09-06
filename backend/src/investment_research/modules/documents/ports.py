"""Application-owned persistence contracts for archive documents."""

from typing import Protocol
from uuid import UUID

from investment_research.modules.documents.domain import (
    Document,
    DocumentVersion,
    OriginalArtifact,
)
from investment_research.modules.identity.domain import WorkspaceContext


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
