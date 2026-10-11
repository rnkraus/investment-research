"""Read-only application service for the workspace's current document catalog."""

from investment_research.modules.documents.contracts import DocumentListPage, DocumentListQuery
from investment_research.modules.documents.ports import DocumentReadUnitOfWorkFactory
from investment_research.modules.identity.domain import WorkspaceContext


class ListDocumentsService:
    """List bounded canonical metadata without opening original storage or providers."""

    def __init__(self, *, unit_of_work_factory: DocumentReadUnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def list_documents(
        self,
        context: WorkspaceContext,
        query: DocumentListQuery,
    ) -> DocumentListPage:
        """Resolve one page in a short-lived, caller-independent read session."""

        with self._unit_of_work_factory() as unit_of_work:
            return unit_of_work.documents.list_current_documents(context, query)
