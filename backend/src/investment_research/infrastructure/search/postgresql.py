"""PostgreSQL adapters for canonical lexical retrieval and dense qualification."""

from collections.abc import Callable
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from investment_research.modules.companies.contracts import CompanySummary
from investment_research.modules.companies.domain import CompanyTicker
from investment_research.modules.companies.persistence import CompanyRow
from investment_research.modules.documents.domain import DocumentLifecycleState, SourceLocatorKind
from investment_research.modules.documents.persistence import (
    DocumentCompanyRow,
    DocumentRow,
    DocumentSourceAttributionRow,
    DocumentVersionRow,
    OriginalArtifactRow,
    document_has_company,
    document_matches_source_attribution,
    publication_date_predicates,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.persistence import (
    ChunkRow,
    ChunkSetRow,
    ProcessingRunRow,
    StructureBlockRow,
)
from investment_research.modules.search.archive import (
    ARCHIVE_PASSAGE_LIMIT,
    ARCHIVE_SEARCH_LIMIT,
    ArchiveDocumentHit,
    ArchiveSearchHit,
    ArchiveSearchResult,
)
from investment_research.modules.search.contracts import (
    DenseQualificationRequest,
    LexicalSearchHit,
    LexicalSearchQuery,
    QualifiedDenseHit,
)
from investment_research.modules.search.domain import RetrievalFilters

TICKER_ADAPTER = TypeAdapter(CompanyTicker)


class PostgresLexicalSearch:
    """Select eligible canonical chunks through PostgreSQL full-text search."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def search(
        self,
        context: WorkspaceContext,
        query: LexicalSearchQuery,
    ) -> tuple[LexicalSearchHit, ...]:
        """Return bounded hits after all canonical eligibility predicates pass."""

        statement = self.build_statement(context, query)
        rows = self._session.execute(statement).tuples()
        return tuple(
            LexicalSearchHit(
                chunk_id=chunk_id,
                document_id=document_id,
                document_version_id=document_version_id,
                structure_block_id=structure_block_id,
                source_locator_kind=source_locator_kind,
                page_number=page_number,
                text=text,
                raw_score=float(raw_score),
            )
            for (
                chunk_id,
                document_id,
                document_version_id,
                structure_block_id,
                source_locator_kind,
                page_number,
                text,
                raw_score,
            ) in rows
        )

    @staticmethod
    def build_statement(
        context: WorkspaceContext,
        query: LexicalSearchQuery,
    ) -> Select[tuple[UUID, UUID, UUID, UUID, SourceLocatorKind, int | None, str, float]]:
        """Build one query that filters canonical state before ranking and limiting."""

        text_query = func.websearch_to_tsquery("english", query.normalized_query)
        raw_score = func.ts_rank_cd(ChunkRow.search_vector, text_query).label("raw_score")

        statement = (
            select(
                ChunkRow.id,
                DocumentVersionRow.document_id,
                ChunkRow.document_version_id,
                ChunkRow.structure_block_id,
                StructureBlockRow.source_locator_kind,
                StructureBlockRow.page_number,
                ChunkRow.text,
                raw_score,
            )
            .select_from(ChunkRow)
            .join(
                ChunkSetRow,
                and_(
                    ChunkSetRow.workspace_id == ChunkRow.workspace_id,
                    ChunkSetRow.id == ChunkRow.chunk_set_id,
                    ChunkSetRow.processing_run_id == ChunkRow.processing_run_id,
                    ChunkSetRow.document_version_id == ChunkRow.document_version_id,
                ),
            )
            .join(
                ProcessingRunRow,
                and_(
                    ProcessingRunRow.workspace_id == ChunkRow.workspace_id,
                    ProcessingRunRow.id == ChunkRow.processing_run_id,
                    ProcessingRunRow.document_version_id == ChunkRow.document_version_id,
                ),
            )
            .join(
                StructureBlockRow,
                and_(
                    StructureBlockRow.workspace_id == ChunkRow.workspace_id,
                    StructureBlockRow.id == ChunkRow.structure_block_id,
                    StructureBlockRow.processing_run_id == ChunkRow.processing_run_id,
                    StructureBlockRow.document_version_id == ChunkRow.document_version_id,
                ),
            )
            .join(
                DocumentVersionRow,
                and_(
                    DocumentVersionRow.workspace_id == ChunkRow.workspace_id,
                    DocumentVersionRow.id == ChunkRow.document_version_id,
                ),
            )
            .join(
                DocumentRow,
                and_(
                    DocumentRow.workspace_id == DocumentVersionRow.workspace_id,
                    DocumentRow.id == DocumentVersionRow.document_id,
                    DocumentRow.current_version_id == DocumentVersionRow.id,
                ),
            )
            .where(
                ChunkRow.workspace_id == context.workspace_id,
                DocumentRow.lifecycle_state == DocumentLifecycleState.ACTIVE,
                ChunkSetRow.is_active.is_(True),
                ChunkSetRow.lexical_config_version == query.lexical_config_version,
                ProcessingRunRow.state.in_(
                    (ProcessingState.LEXICAL_READY, ProcessingState.SEARCHABLE)
                ),
                ProcessingRunRow.lexical_config_version == query.lexical_config_version,
                ChunkRow.search_vector.bool_op("@@")(text_query),
                raw_score > 0,
                *publication_date_predicates(query.filters),
            )
        )

        if query.filters.document_ids:
            statement = statement.where(DocumentRow.id.in_(query.filters.document_ids))
        if query.filters.company_id is not None:
            statement = statement.where(document_has_company(query.filters.company_id))
        if (attribution := query.filters.source_attribution) is not None:
            statement = statement.outerjoin_from(
                DocumentVersionRow, DocumentSourceAttributionRow
            ).where(
                document_matches_source_attribution(
                    attribution, query.filters.source_attribution_kind
                )
            )
        if query.filters.available_at_cutoff is not None:
            statement = statement.where(
                DocumentVersionRow.available_at.is_not(None),
                DocumentVersionRow.available_at <= query.filters.available_at_cutoff,
            )
        if query.filters.captured_at_cutoff is not None:
            statement = statement.where(
                DocumentVersionRow.captured_at <= query.filters.captured_at_cutoff,
            )

        return statement.order_by(raw_score.desc(), ChunkRow.id.asc()).limit(query.candidate_limit)


class PostgresArchiveSearchReader:
    """Read passage metadata together with canonical search eligibility."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def search(
        self, context: WorkspaceContext, query: LexicalSearchQuery, *, metadata_term: str
    ) -> ArchiveSearchResult:
        statement = (
            PostgresLexicalSearch.build_statement(context, query)
            .add_columns(DocumentVersionRow.title, DocumentVersionRow.published_at)
            .join(
                OriginalArtifactRow,
                and_(
                    OriginalArtifactRow.workspace_id == DocumentVersionRow.workspace_id,
                    OriginalArtifactRow.document_version_id == DocumentVersionRow.id,
                ),
            )
        )
        with self._session_factory() as session:
            rows = session.execute(statement).tuples().all()
            items = tuple(
                ArchiveSearchHit(
                    chunk_id=chunk_id,
                    document_id=document_id,
                    document_version_id=document_version_id,
                    title=title,
                    published_at=published_at,
                    source_locator_kind=locator,
                    page_number=page_number,
                    passage=text[:ARCHIVE_PASSAGE_LIMIT],
                )
                for (
                    chunk_id,
                    document_id,
                    document_version_id,
                    _,
                    locator,
                    page_number,
                    text,
                    _,
                    title,
                    published_at,
                ) in rows[:ARCHIVE_SEARCH_LIMIT]
            )
            return ArchiveSearchResult(
                companies=self._find_companies(session, context, query.filters, metadata_term),
                documents=self._find_documents(session, context, query.filters, metadata_term),
                items=items,
                has_more=len(rows) > ARCHIVE_SEARCH_LIMIT,
            )

    def search_semantic(
        self, context: WorkspaceContext, request: DenseQualificationRequest
    ) -> ArchiveSearchResult:
        if not request.provider_hits:
            return ArchiveSearchResult(companies=(), documents=(), items=(), has_more=False)
        statement = (
            PostgresDenseCandidateQualifier.build_statement(context, request)
            .add_columns(DocumentVersionRow.title, DocumentVersionRow.published_at)
            .join(
                OriginalArtifactRow,
                and_(
                    OriginalArtifactRow.workspace_id == DocumentVersionRow.workspace_id,
                    OriginalArtifactRow.document_version_id == DocumentVersionRow.id,
                ),
            )
        )
        with self._session_factory() as session:
            by_chunk_id = {
                chunk_id: ArchiveSearchHit(
                    chunk_id=chunk_id,
                    document_id=document_id,
                    document_version_id=document_version_id,
                    title=title,
                    published_at=published_at,
                    source_locator_kind=locator,
                    page_number=page_number,
                    passage=text[:ARCHIVE_PASSAGE_LIMIT],
                )
                for (
                    chunk_id,
                    document_id,
                    document_version_id,
                    _,
                    locator,
                    page_number,
                    text,
                    title,
                    published_at,
                ) in session.execute(statement).tuples()
            }
        items = tuple(
            by_chunk_id[hit.chunk_id]
            for hit in request.provider_hits
            if hit.chunk_id in by_chunk_id
        )
        return ArchiveSearchResult(
            companies=(),
            documents=(),
            items=items[:ARCHIVE_SEARCH_LIMIT],
            has_more=len(items) > ARCHIVE_SEARCH_LIMIT,
        )

    @staticmethod
    def _find_companies(
        session: Session, context: WorkspaceContext, filters: RetrievalFilters, term: str
    ) -> tuple[CompanySummary, ...]:
        try:
            ticker = TICKER_ADAPTER.validate_python(term)
        except ValidationError:
            return ()
        statement = (
            select(CompanyRow)
            .where(CompanyRow.workspace_id == context.workspace_id, CompanyRow.ticker == ticker)
            .order_by(CompanyRow.normalized_name, CompanyRow.id)
            .limit(ARCHIVE_SEARCH_LIMIT)
        )
        if filters.company_id is not None:
            statement = statement.where(CompanyRow.id == filters.company_id)
        if (attribution := filters.source_attribution) is not None:
            matching_document = (
                select(DocumentRow.id)
                .select_from(DocumentCompanyRow)
                .join(
                    DocumentRow,
                    and_(
                        DocumentRow.workspace_id == DocumentCompanyRow.workspace_id,
                        DocumentRow.id == DocumentCompanyRow.document_id,
                    ),
                )
                .join(
                    DocumentVersionRow,
                    and_(
                        DocumentVersionRow.workspace_id == DocumentRow.workspace_id,
                        DocumentVersionRow.id == DocumentRow.current_version_id,
                    ),
                )
                .outerjoin_from(DocumentVersionRow, DocumentSourceAttributionRow)
                .where(
                    DocumentCompanyRow.workspace_id == CompanyRow.workspace_id,
                    DocumentCompanyRow.company_id == CompanyRow.id,
                    DocumentRow.lifecycle_state == DocumentLifecycleState.ACTIVE,
                    document_matches_source_attribution(
                        attribution, filters.source_attribution_kind
                    ),
                    *publication_date_predicates(filters),
                )
                .exists()
            )
            statement = statement.where(matching_document)
        return tuple(
            CompanySummary.from_company(row.to_domain()) for row in session.scalars(statement)
        )

    @staticmethod
    def _find_documents(
        session: Session, context: WorkspaceContext, filters: RetrievalFilters, term: str
    ) -> tuple[ArchiveDocumentHit, ...]:
        if not term:
            return ()
        statement = (
            select(
                DocumentRow.id.label("document_id"),
                DocumentVersionRow.id.label("document_version_id"),
                DocumentVersionRow.title,
                DocumentVersionRow.published_at,
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
            .where(
                DocumentRow.workspace_id == context.workspace_id,
                DocumentRow.lifecycle_state == DocumentLifecycleState.ACTIVE,
                DocumentVersionRow.title.icontains(term, autoescape=True),
                *publication_date_predicates(filters),
            )
            .order_by(
                (func.lower(DocumentVersionRow.title) == func.lower(term)).desc(),
                DocumentVersionRow.title,
                DocumentRow.id,
            )
            .limit(ARCHIVE_SEARCH_LIMIT)
        )
        if filters.company_id is not None:
            statement = statement.where(document_has_company(filters.company_id))
        if (attribution := filters.source_attribution) is not None:
            statement = statement.outerjoin_from(
                DocumentVersionRow, DocumentSourceAttributionRow
            ).where(
                document_matches_source_attribution(attribution, filters.source_attribution_kind)
            )
        return tuple(
            ArchiveDocumentHit.model_validate(row) for row in session.execute(statement).mappings()
        )


class PostgresDenseCandidateQualifier:
    """Rehydrate dense provider hits only through eligible canonical state."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def qualify(
        self,
        context: WorkspaceContext,
        request: DenseQualificationRequest,
    ) -> tuple[QualifiedDenseHit, ...]:
        """Return eligible hits in provider order without disclosing rejected IDs."""

        if not request.provider_hits:
            return ()
        rows = self._session.execute(self.build_statement(context, request)).tuples()
        canonical_by_chunk_id = {
            chunk_id: (
                document_id,
                document_version_id,
                structure_block_id,
                source_locator_kind,
                page_number,
                text,
            )
            for (
                chunk_id,
                document_id,
                document_version_id,
                structure_block_id,
                source_locator_kind,
                page_number,
                text,
            ) in rows
        }
        qualified: list[QualifiedDenseHit] = []
        for provider_hit in request.provider_hits:
            canonical = canonical_by_chunk_id.get(provider_hit.chunk_id)
            if canonical is None:
                continue
            (
                document_id,
                document_version_id,
                structure_block_id,
                source_locator_kind,
                page_number,
                text,
            ) = canonical
            qualified.append(
                QualifiedDenseHit(
                    chunk_id=provider_hit.chunk_id,
                    document_id=document_id,
                    document_version_id=document_version_id,
                    structure_block_id=structure_block_id,
                    source_locator_kind=source_locator_kind,
                    page_number=page_number,
                    text=text,
                    raw_score=provider_hit.raw_score,
                )
            )
        return tuple(qualified)

    @staticmethod
    def build_statement(
        context: WorkspaceContext,
        request: DenseQualificationRequest,
    ) -> Select[tuple[UUID, UUID, UUID, UUID, SourceLocatorKind, int | None, str]]:
        """Build one query that rejects stale, foreign, or misconfigured vector hits."""

        statement = (
            select(
                ChunkRow.id,
                DocumentVersionRow.document_id,
                ChunkRow.document_version_id,
                ChunkRow.structure_block_id,
                StructureBlockRow.source_locator_kind,
                StructureBlockRow.page_number,
                ChunkRow.text,
            )
            .select_from(ChunkRow)
            .join(
                ChunkSetRow,
                and_(
                    ChunkSetRow.workspace_id == ChunkRow.workspace_id,
                    ChunkSetRow.id == ChunkRow.chunk_set_id,
                    ChunkSetRow.processing_run_id == ChunkRow.processing_run_id,
                    ChunkSetRow.document_version_id == ChunkRow.document_version_id,
                ),
            )
            .join(
                ProcessingRunRow,
                and_(
                    ProcessingRunRow.workspace_id == ChunkRow.workspace_id,
                    ProcessingRunRow.id == ChunkRow.processing_run_id,
                    ProcessingRunRow.document_version_id == ChunkRow.document_version_id,
                ),
            )
            .join(
                StructureBlockRow,
                and_(
                    StructureBlockRow.workspace_id == ChunkRow.workspace_id,
                    StructureBlockRow.id == ChunkRow.structure_block_id,
                    StructureBlockRow.processing_run_id == ChunkRow.processing_run_id,
                    StructureBlockRow.document_version_id == ChunkRow.document_version_id,
                ),
            )
            .join(
                DocumentVersionRow,
                and_(
                    DocumentVersionRow.workspace_id == ChunkRow.workspace_id,
                    DocumentVersionRow.id == ChunkRow.document_version_id,
                ),
            )
            .join(
                DocumentRow,
                and_(
                    DocumentRow.workspace_id == DocumentVersionRow.workspace_id,
                    DocumentRow.id == DocumentVersionRow.document_id,
                    DocumentRow.current_version_id == DocumentVersionRow.id,
                ),
            )
            .where(
                ChunkRow.workspace_id == context.workspace_id,
                ChunkRow.id.in_(hit.chunk_id for hit in request.provider_hits),
                DocumentRow.lifecycle_state == DocumentLifecycleState.ACTIVE,
                ChunkSetRow.is_active.is_(True),
                ChunkSetRow.lexical_config_version == request.lexical_config_version,
                ProcessingRunRow.state == ProcessingState.SEARCHABLE,
                ProcessingRunRow.lexical_config_version == request.lexical_config_version,
                ProcessingRunRow.embedding_config_version == request.embedding_config_version,
                ProcessingRunRow.dense_index_config_version == request.dense_index_config_version,
                *publication_date_predicates(request.filters),
            )
        )

        if request.filters.document_ids:
            statement = statement.where(DocumentRow.id.in_(request.filters.document_ids))
        if request.filters.company_id is not None:
            statement = statement.where(document_has_company(request.filters.company_id))
        if (attribution := request.filters.source_attribution) is not None:
            statement = statement.outerjoin_from(
                DocumentVersionRow, DocumentSourceAttributionRow
            ).where(
                document_matches_source_attribution(
                    attribution, request.filters.source_attribution_kind
                )
            )
        if request.filters.available_at_cutoff is not None:
            statement = statement.where(
                DocumentVersionRow.available_at.is_not(None),
                DocumentVersionRow.available_at <= request.filters.available_at_cutoff,
            )
        if request.filters.captured_at_cutoff is not None:
            statement = statement.where(
                DocumentVersionRow.captured_at <= request.filters.captured_at_cutoff,
            )
        return statement
