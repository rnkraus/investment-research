"""Application contract for owner corrections to captured source attribution."""

from typing import Protocol
from uuid import UUID

from investment_research.modules.documents.contracts import ImmutableContract
from investment_research.modules.documents.domain import SourceAuthorName, SourceXHandle
from investment_research.modules.identity.domain import WorkspaceContext


class ReplaceDocumentSourceAttribution(ImmutableContract):
    """Replace both display fields for the version observed in the archive."""

    document_version_id: UUID
    author_name: SourceAuthorName | None
    x_handle: SourceXHandle | None


class DocumentSourceAttribution(ReplaceDocumentSourceAttribution):
    """The effective attribution saved for the requested document version."""

    document_id: UUID


class SourceAttributionUnavailableError(Exception):
    """The document is absent or excluded from the workspace archive."""


class SourceAttributionConflictError(Exception):
    """The current document version has changed since it was opened."""


class DocumentSourceAttributionWriter(Protocol):
    """Save owner-supplied display metadata without rewriting captured content."""

    def replace(
        self,
        context: WorkspaceContext,
        document_id: UUID,
        command: ReplaceDocumentSourceAttribution,
    ) -> DocumentSourceAttribution: ...
