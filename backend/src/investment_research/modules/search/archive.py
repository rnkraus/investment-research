"""Discovery of canonical passages without creating research requests."""

from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, StringConstraints, ValidationError, model_validator

from investment_research.modules.companies.contracts import CompanySummary
from investment_research.modules.documents.domain import (
    DocumentTitle,
    SourceAttributionScope,
    SourceLocatorKind,
)
from investment_research.modules.documents.publication import PublicationDateBounds
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.application import normalize_lexical_query
from investment_research.modules.search.contracts import (
    DenseQualificationRequest,
    DenseRetrievalProfile,
    DenseRetrievalQuery,
    ImmutableSearchContract,
    LexicalSearchHit,
    LexicalSearchQuery,
    QualifiedDenseHit,
)
from investment_research.modules.search.domain import ConfigurationVersion, RetrievalFilters
from investment_research.modules.search.fusion import fuse_rrf_hits
from investment_research.modules.search.ports import (
    DenseRetriever,
    SearchProviderContractError,
    SearchProviderUnavailableError,
)
from investment_research.modules.search.query_embedding import AccountedQueryEmbedding

ARCHIVE_SEARCH_LIMIT = 20
ARCHIVE_PASSAGE_LIMIT = 4_000


class ArchiveSearchInput(ImmutableSearchContract, PublicationDateBounds, SourceAttributionScope):
    """One archive query with optional company and publication filters."""

    query: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=300, pattern=r"^[^\x00]+$"
        ),
    ]
    company_id: UUID | None = None
    mode: Literal["keyword", "semantic", "hybrid"] = "keyword"

    @model_validator(mode="after")
    def validate_lexical_query(self) -> Self:
        if self.mode != "semantic":
            normalize_lexical_query(self.query)
        return self


class ArchiveDocumentHit(ImmutableSearchContract):
    """A current document whose title matches the query."""

    document_id: UUID
    document_version_id: UUID
    title: DocumentTitle
    published_at: AwareDatetime | None


class ArchiveSearchHit(ImmutableSearchContract):
    """A passage preview and the identifiers needed to open its current original."""

    chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    title: DocumentTitle
    published_at: AwareDatetime | None
    source_locator_kind: SourceLocatorKind
    page_number: int | None = Field(ge=1)
    passage: str = Field(min_length=1, max_length=ARCHIVE_PASSAGE_LIMIT)

    @model_validator(mode="after")
    def require_media_appropriate_locator(self) -> Self:
        if (self.source_locator_kind is SourceLocatorKind.PDF_PAGE) != (
            self.page_number is not None
        ):
            raise ValueError("only PDF hits have a physical page number")
        return self


class ArchiveSearchResult(ImmutableSearchContract):
    """Bounded result groups. The has_more flag refers to passage matches."""

    companies: tuple[CompanySummary, ...] = Field(max_length=ARCHIVE_SEARCH_LIMIT)
    documents: tuple[ArchiveDocumentHit, ...] = Field(max_length=ARCHIVE_SEARCH_LIMIT)
    items: tuple[ArchiveSearchHit, ...] = Field(max_length=ARCHIVE_SEARCH_LIMIT)
    has_more: bool


@dataclass(frozen=True, slots=True)
class ArchiveSearchCandidate[Hit: LexicalSearchHit | QualifiedDenseHit]:
    """A qualified canonical hit with the metadata required by archive discovery."""

    hit: Hit
    title: DocumentTitle
    published_at: AwareDatetime | None

    def to_result(self) -> ArchiveSearchHit:
        return ArchiveSearchHit(
            chunk_id=self.hit.chunk_id,
            document_id=self.hit.document_id,
            document_version_id=self.hit.document_version_id,
            title=self.title,
            published_at=self.published_at,
            source_locator_kind=self.hit.source_locator_kind,
            page_number=self.hit.page_number,
            passage=self.hit.text[:ARCHIVE_PASSAGE_LIMIT],
        )


@dataclass(frozen=True, slots=True)
class ArchiveSearchCandidates:
    """Detached lane rankings and metadata from one canonical database read scope."""

    lexical: tuple[ArchiveSearchCandidate[LexicalSearchHit], ...]
    dense: tuple[ArchiveSearchCandidate[QualifiedDenseHit], ...]
    companies: tuple[CompanySummary, ...]
    documents: tuple[ArchiveDocumentHit, ...]


class ArchiveSearchReader(Protocol):
    def read(
        self,
        context: WorkspaceContext,
        *,
        query: LexicalSearchQuery | None,
        request: DenseQualificationRequest | None,
        metadata_term: str | None,
    ) -> ArchiveSearchCandidates:
        """Load qualified lanes and optional exact metadata without choosing fusion policy."""


@dataclass(frozen=True, slots=True)
class ArchiveSemanticRetrieval:
    """Embed a search and get bounded candidates before opening a read transaction."""

    embedding: AccountedQueryEmbedding
    retriever: DenseRetriever
    profile: DenseRetrievalProfile

    def retrieve(
        self,
        context: WorkspaceContext,
        *,
        text: str,
        filters: RetrievalFilters,
        lexical_config_version: ConfigurationVersion,
    ) -> DenseQualificationRequest:
        embedding = self.embedding.embed(
            context,
            text=text,
            configuration_version=self.profile.embedding_config_version,
            correlation_id=uuid4(),
            research_request_id=None,
        )
        query = DenseRetrievalQuery(
            query_vector=embedding.vectors[0].values,
            filters=filters,
            embedding_config_version=self.profile.embedding_config_version,
            dense_index_config_version=self.profile.dense_index_config_version,
            candidate_limit=min(self.profile.candidate_limit, ARCHIVE_SEARCH_LIMIT + 1),
        )
        hits = tuple(self.retriever.retrieve(context, query))
        if len(hits) > query.candidate_limit:
            raise SearchProviderContractError("archive vector candidate limit exceeded")
        try:
            return DenseQualificationRequest(
                provider_hits=hits,
                filters=filters,
                lexical_config_version=lexical_config_version,
                embedding_config_version=query.embedding_config_version,
                dense_index_config_version=query.dense_index_config_version,
            )
        except ValidationError:
            raise SearchProviderContractError("invalid archive vector candidates") from None


class SearchArchiveService:
    def __init__(
        self,
        *,
        reader: ArchiveSearchReader,
        lexical_config_version: ConfigurationVersion,
        semantic: ArchiveSemanticRetrieval | None = None,
    ) -> None:
        self._reader = reader
        self._lexical_config_version = lexical_config_version
        self._semantic = semantic

    def search(self, context: WorkspaceContext, command: ArchiveSearchInput) -> ArchiveSearchResult:
        filters = RetrievalFilters(
            company_id=command.company_id,
            source_attribution=command.source_attribution,
            source_attribution_kind=command.source_attribution_kind,
            published_from=command.published_from,
            published_to=command.published_to,
        )
        request = None
        if command.mode in {"semantic", "hybrid"}:
            if self._semantic is None:
                raise SearchProviderUnavailableError("semantic archive search is unavailable")
            request = self._semantic.retrieve(
                context,
                text=command.query,
                filters=filters,
                lexical_config_version=self._lexical_config_version,
            )
        query = None
        term = None
        if command.mode != "semantic":
            query = LexicalSearchQuery(
                normalized_query=normalize_lexical_query(command.query),
                filters=filters,
                lexical_config_version=self._lexical_config_version,
                candidate_limit=ARCHIVE_SEARCH_LIMIT + 1,
            )
            term = command.query
            if term.startswith('"') and term.endswith('"'):
                term = term[1:-1].strip()
        candidates = self._reader.read(context, query=query, request=request, metadata_term=term)
        if command.mode == "hybrid":
            ranked = fuse_rrf_hits(
                lexical_hits=tuple(item.hit for item in candidates.lexical[:ARCHIVE_SEARCH_LIMIT]),
                dense_hits=tuple(item.hit for item in candidates.dense[:ARCHIVE_SEARCH_LIMIT]),
            )
            combined: tuple[
                ArchiveSearchCandidate[QualifiedDenseHit]
                | ArchiveSearchCandidate[LexicalSearchHit],
                ...,
            ] = (*candidates.dense, *candidates.lexical)
            by_chunk = {item.hit.chunk_id: item.to_result() for item in combined}
            items = tuple(by_chunk[item.hit.chunk_id] for item in ranked)
        else:
            lane = candidates.dense if command.mode == "semantic" else candidates.lexical
            items = tuple(item.to_result() for item in lane)
        return ArchiveSearchResult(
            companies=candidates.companies,
            documents=candidates.documents,
            items=items[:ARCHIVE_SEARCH_LIMIT],
            has_more=(
                len(items) > ARCHIVE_SEARCH_LIMIT
                or len(candidates.lexical) > ARCHIVE_SEARCH_LIMIT
                or len(candidates.dense) > ARCHIVE_SEARCH_LIMIT
            ),
        )
