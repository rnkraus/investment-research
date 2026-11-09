"""Read-only application service for the workspace's current document catalog."""

from investment_research.modules.documents.contracts import (
    DocumentListPage,
    DocumentListQuery,
    SourceAttributionLookup,
    SourceAttributionPage,
)
from investment_research.modules.documents.ports import DocumentCatalogReader
from investment_research.modules.identity.domain import WorkspaceContext


class ListDocumentsService:
    """List bounded canonical metadata without opening original storage or providers."""

    def __init__(self, *, catalog: DocumentCatalogReader) -> None:
        self._catalog = catalog

    def list_documents(
        self,
        context: WorkspaceContext,
        query: DocumentListQuery,
    ) -> DocumentListPage:
        """Resolve one page in a short-lived, caller-independent read session."""

        return self._catalog.list_current_documents(context, query)

    def find_source_attributions(
        self, context: WorkspaceContext, query: SourceAttributionLookup
    ) -> SourceAttributionPage:
        return self._catalog.find_source_attributions(context, query)
