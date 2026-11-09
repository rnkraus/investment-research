"""Canonical PostgreSQL projection for the document catalog."""

from collections.abc import Callable

from sqlalchemy import and_, func, literal, or_, select, true, union_all
from sqlalchemy.orm import Session

from investment_research.modules.documents.contracts import (
    SOURCE_ATTRIBUTION_PAGE_SIZE,
    DocumentListCursor,
    DocumentListEntry,
    DocumentListPage,
    DocumentListQuery,
    DocumentProcessingSnapshot,
    SourceAttributionChoice,
    SourceAttributionLookup,
    SourceAttributionPage,
)
from investment_research.modules.documents.domain import DocumentLifecycleState
from investment_research.modules.documents.persistence import (
    DocumentRow,
    DocumentSourceAttributionRow,
    DocumentVersionRow,
    OriginalArtifactRow,
    document_has_company,
    document_matches_source_attribution,
    document_source_attribution_columns,
    publication_date_predicates,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.persistence import (
    ProcessingRunRow,
    latest_processing_run_order,
)


class PostgresDocumentCatalogReader:
    """Read one scoped catalog page and its status in a short-lived session."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def list_current_documents(
        self,
        context: WorkspaceContext,
        query: DocumentListQuery,
    ) -> DocumentListPage:
        """Filter current documents and show the same latest run used for filtering."""

        latest = (
            select(ProcessingRunRow.id, ProcessingRunRow.state, ProcessingRunRow.error_summary)
            .where(
                ProcessingRunRow.workspace_id == DocumentVersionRow.workspace_id,
                ProcessingRunRow.document_version_id == DocumentVersionRow.id,
            )
            .correlate(DocumentVersionRow)
            .order_by(*latest_processing_run_order())
            .limit(1)
            .lateral("latest_processing")
        )
        author_name, x_handle = document_source_attribution_columns()
        statement = (
            select(
                DocumentRow.id.label("document_id"),
                DocumentVersionRow.id.label("document_version_id"),
                DocumentVersionRow.title,
                DocumentVersionRow.language,
                author_name,
                x_handle,
                DocumentVersionRow.published_at,
                DocumentVersionRow.available_at,
                DocumentVersionRow.captured_at,
                DocumentRow.created_at,
                OriginalArtifactRow.media_type,
                OriginalArtifactRow.byte_size,
                latest.c.id.label("run_id"),
                latest.c.state.label("run_state"),
                latest.c.error_summary.label("run_error"),
            )
            .select_from(DocumentRow)
            .join(
                DocumentVersionRow,
                and_(
                    DocumentVersionRow.workspace_id == DocumentRow.workspace_id,
                    DocumentVersionRow.document_id == DocumentRow.id,
                    DocumentVersionRow.id == DocumentRow.current_version_id,
                ),
            )
            .join(
                OriginalArtifactRow,
                and_(
                    OriginalArtifactRow.workspace_id == DocumentVersionRow.workspace_id,
                    OriginalArtifactRow.document_version_id == DocumentVersionRow.id,
                ),
            )
            .outerjoin_from(DocumentVersionRow, DocumentSourceAttributionRow)
            .outerjoin(latest, true())
            .where(
                DocumentRow.workspace_id == context.workspace_id,
                DocumentVersionRow.workspace_id == context.workspace_id,
                OriginalArtifactRow.workspace_id == context.workspace_id,
                DocumentRow.lifecycle_state == DocumentLifecycleState.ACTIVE,
                *publication_date_predicates(query),
            )
            .order_by(DocumentRow.created_at.desc(), DocumentRow.id.desc())
            .limit(query.page_size + 1)
        )
        if query.company_id is not None:
            statement = statement.where(document_has_company(query.company_id))
        if query.media_type is not None:
            statement = statement.where(OriginalArtifactRow.media_type == query.media_type)
        if query.processing_state == "NO_STATUS":
            statement = statement.where(latest.c.id.is_(None))
        elif query.processing_state is not None:
            statement = statement.where(latest.c.state == query.processing_state)
        if (attribution := query.source_attribution) is not None:
            statement = statement.where(
                document_matches_source_attribution(attribution, query.source_attribution_kind)
            )
        if query.after is not None:
            statement = statement.where(
                or_(
                    DocumentRow.created_at < query.after.created_at,
                    and_(
                        DocumentRow.created_at == query.after.created_at,
                        DocumentRow.id < query.after.document_id,
                    ),
                )
            )

        with self._session_factory() as session:
            rows = session.execute(statement).mappings().all()
        entries: list[DocumentListEntry] = []
        for row in rows[: query.page_size]:
            values = dict(row)
            run_id = values.pop("run_id")
            run_state = values.pop("run_state")
            run_error = values.pop("run_error")
            if run_id is not None:
                values["processing"] = DocumentProcessingSnapshot(
                    processing_run_id=run_id,
                    document_version_id=values["document_version_id"],
                    state=run_state,
                    error_summary=run_error,
                )
            entries.append(DocumentListEntry.model_validate(values))
        items = tuple(entries)
        next_cursor = None
        if len(rows) > query.page_size:
            last = items[-1]
            next_cursor = DocumentListCursor(
                created_at=last.created_at,
                document_id=last.document_id,
            )
        return DocumentListPage(items=items, next_cursor=next_cursor)

    def find_source_attributions(
        self, context: WorkspaceContext, query: SourceAttributionLookup
    ) -> SourceAttributionPage:
        """Read a stable page of distinct names and handles from active versions."""

        author, handle = document_source_attribution_columns()
        sources = (
            select(author, handle)
            .select_from(DocumentRow)
            .join(
                DocumentVersionRow,
                and_(
                    DocumentVersionRow.workspace_id == DocumentRow.workspace_id,
                    DocumentVersionRow.id == DocumentRow.current_version_id,
                ),
            )
            .join_from(DocumentVersionRow, OriginalArtifactRow)
            .outerjoin_from(DocumentVersionRow, DocumentSourceAttributionRow)
            .where(
                DocumentRow.workspace_id == context.workspace_id,
                DocumentRow.lifecycle_state == DocumentLifecycleState.ACTIVE,
            )
            .cte("active_source_attributions")
        )
        choices = union_all(
            select(literal("author").label("kind"), sources.c.author_name.label("value")),
            select(
                literal("handle").label("kind"),
                func.concat("@", func.ltrim(sources.c.x_handle, "@")).label("value"),
            ).where(sources.c.x_handle.is_not(None)),
        ).subquery()
        key = func.concat(choices.c.kind, ":", func.lower(choices.c.value)).collate("C")
        statement = (
            select(choices.c.kind, func.min(choices.c.value.collate("C")), key)
            .where(choices.c.value.is_not(None))
            .group_by(choices.c.kind, key)
            .order_by(key)
            .limit(SOURCE_ATTRIBUTION_PAGE_SIZE + 1)
        )
        if query.q is not None:
            statement = statement.where(choices.c.value.icontains(query.q, autoescape=True))
            if query.q.startswith("@"):
                statement = statement.where(choices.c.kind == "handle")
        if query.cursor is not None:
            statement = statement.where(key > query.cursor)
        with self._session_factory() as session:
            rows = session.execute(statement).tuples().all()
        page = rows[:SOURCE_ATTRIBUTION_PAGE_SIZE]
        return SourceAttributionPage(
            items=tuple(SourceAttributionChoice(kind=kind, value=value) for kind, value, _ in page),
            next_cursor=page[-1][2] if len(rows) > SOURCE_ATTRIBUTION_PAGE_SIZE else None,
        )
