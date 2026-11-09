"""PostgreSQL storage for owner-edited source attribution."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from investment_research.infrastructure.database.session import (
    SessionFactory,
    transactional_session,
)
from investment_research.modules.documents.domain import DocumentLifecycleState
from investment_research.modules.documents.persistence import (
    DocumentRow,
    DocumentSourceAttributionRow,
)
from investment_research.modules.documents.source_attribution import (
    DocumentSourceAttribution,
    ReplaceDocumentSourceAttribution,
    SourceAttributionConflictError,
    SourceAttributionUnavailableError,
)
from investment_research.modules.identity.domain import WorkspaceContext


class PostgresDocumentSourceAttributionWriter:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def replace(
        self,
        context: WorkspaceContext,
        document_id: UUID,
        command: ReplaceDocumentSourceAttribution,
    ) -> DocumentSourceAttribution:
        with transactional_session(self._session_factory) as session:
            version_id = session.scalar(
                select(DocumentRow.current_version_id)
                .where(
                    DocumentRow.workspace_id == context.workspace_id,
                    DocumentRow.id == document_id,
                    DocumentRow.lifecycle_state == DocumentLifecycleState.ACTIVE,
                )
                .with_for_update()
            )
            if version_id is None:
                raise SourceAttributionUnavailableError
            if version_id != command.document_version_id:
                raise SourceAttributionConflictError

            fields = {"author_name": command.author_name, "x_handle": command.x_handle}
            session.execute(
                insert(DocumentSourceAttributionRow)
                .values(workspace_id=context.workspace_id, document_version_id=version_id, **fields)
                .on_conflict_do_update(
                    index_elements=[
                        DocumentSourceAttributionRow.workspace_id,
                        DocumentSourceAttributionRow.document_version_id,
                    ],
                    set_=fields,
                )
            )

        return DocumentSourceAttribution(document_id=document_id, **command.model_dump())
