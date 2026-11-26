"""Research retrieval orchestration and its M1 provider policy."""

import re
from collections.abc import Callable, Set
from dataclasses import dataclass
from datetime import UTC, datetime
from time import sleep
from typing import Literal
from uuid import UUID, uuid4

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.application import (
    ExecuteProviderOperationService,
    ProviderBudgetExceededError,
    ProviderUsageExceededReservationError,
    trace_operation,
)
from investment_research.modules.operations.ports import OperationSpan, OperationTracer
from investment_research.modules.research.company_priority import CompanyPriorityReader
from investment_research.modules.research.contracts import (
    CanonicalCandidateSource,
    QueryDecompositionMode,
    ResearchProgressStage,
    ResearchRetrievalInput,
    ResearchRetrievalResult,
)
from investment_research.modules.research.domain import ResearchRequest, TemporalMode
from investment_research.modules.research.errors import ResearchProviderUnavailableError
from investment_research.modules.research.luna_reranking import (
    LUNA_RERANK_POOL_LIMIT,
    AccountedLunaReranker,
)
from investment_research.modules.research.ports import ResearchRetrievalUnitOfWorkFactory
from investment_research.modules.research.query_planning import (
    AccountedQueryPlanner,
    has_explicit_multiple_search_goals,
    search_lexical_queries,
)
from investment_research.modules.research.reranking import AccountedRerankingService
from investment_research.modules.search.application import build_lexical_search_query
from investment_research.modules.search.company_metric_context import (
    best_company_metric_passage,
    company_metric_comparison,
    reserve_company_metric_passages,
)
from investment_research.modules.search.contracts import (
    M1_LANE_CANDIDATE_LIMIT,
    M1_PROVIDER_MAX_ATTEMPTS,
    M1_PROVIDER_RETRY_DELAY_SECONDS,
    DenseQualificationRequest,
    DenseRetrievalProfile,
    DenseRetrievalQuery,
    LexicalSearchHit,
    LexicalSearchProfile,
    LexicalSearchQuery,
    M1SelectionProfile,
    QualifiedDenseHit,
    SelectedRetrievalCandidate,
)
from investment_research.modules.search.domain import (
    LEXICAL_ORDER_SELECTOR_CONFIG_VERSION,
    RetrievalCandidate,
    RetrievalFilters,
    RetrievalMode,
    RetrievalOrigin,
    RetrievalRun,
)
from investment_research.modules.search.fusion import fit_rrf_context, fuse_rrf_hits
from investment_research.modules.search.fusion_contracts import (
    COMPANY_PAGE_RRF_SELECTOR_VERSION,
    LUNA_RERANK_SELECTOR_VERSION,
    RrfFusedCandidate,
    RrfSelectionProfile,
)
from investment_research.modules.search.observation_dates import (
    ends_with_observation_date,
    requested_observation_date,
)
from investment_research.modules.search.page_context import (
    PAGE_SELECTOR_VERSION,
    PDF_NEIGHBOR_ANCHOR_LIMIT,
    add_observation_context,
    add_page_context,
    add_pdf_neighbor,
    observation_metric,
)
from investment_research.modules.search.ports import (
    DenseRetriever,
    EmbeddingProvider,
    LexicalSearch,
    SearchProviderContractError,
    SearchProviderError,
    SearchProviderTransientError,
)
from investment_research.modules.search.quarter_context import (
    QUARTER_COMPARISON_DOCUMENT_LIMIT,
    ReportingQuarter,
    add_quarter_context,
    comparison_quarters,
    report_quarter,
    revenue_forecast_actual_passages,
    revenue_forecast_actual_request,
)
from investment_research.modules.search.query_embedding import AccountedQueryEmbedding
from investment_research.modules.search.query_embedding import (
    QueryEmbeddingAccountingProfile as QueryEmbeddingAccountingProfile,
)
from investment_research.modules.search.reranking import prepare_rrf_reranking
from investment_research.modules.search.reranking_contracts import (
    RrfRerankingProfile,
    RrfRerankingSelectionResult,
)
from investment_research.modules.search.selection import select_m1_context
from investment_research.modules.search.text_neighbors import (
    TEXT_NEIGHBOR_ANCHOR_LIMIT,
    TEXT_NEIGHBOR_CONTEXT_LIMIT,
    TEXT_NEIGHBOR_SELECTOR_VERSION,
    select_text_neighbors,
)

type Clock = Callable[[], datetime]
type IdentifierFactory = Callable[[], UUID]
type Sleeper = Callable[[float], None]
type CanonicalRetrievalHit = LexicalSearchHit | QualifiedDenseHit


@dataclass(frozen=True, slots=True)
class M1QueryRetrievalDependencies:
    """Complete provider and policy bundle required by M1 dual-lane retrieval."""

    embedding_provider: EmbeddingProvider
    dense_retriever: DenseRetriever
    dense_profile: DenseRetrievalProfile
    selection_profile: M1SelectionProfile | RrfSelectionProfile | RrfRerankingProfile
    provider_operation_executor: ExecuteProviderOperationService
    embedding_accounting: QueryEmbeddingAccountingProfile
    retry_delay_seconds: float = M1_PROVIDER_RETRY_DELAY_SECONDS
    sleeper: Sleeper = sleep
    reranking: AccountedRerankingService | None = None

    def __post_init__(self) -> None:
        """Keep injected retry timing inside the frozen M1 policy."""

        if not 0 <= self.retry_delay_seconds <= M1_PROVIDER_RETRY_DELAY_SECONDS:
            raise ValueError("query provider retry delay must be between 0 and 0.25 seconds")
        if isinstance(self.selection_profile, RrfRerankingProfile) != (self.reranking is not None):
            raise ValueError("reranking policy and accounted service must be configured together")


def build_effective_retrieval_filters(
    retrieval_input: ResearchRetrievalInput,
) -> RetrievalFilters:
    """Translate a research time mode into the exact predicates Search must execute."""

    available_at_cutoff = None
    captured_at_cutoff = None
    if retrieval_input.temporal_mode is TemporalMode.PUBLIC_KNOWLEDGE:
        available_at_cutoff = retrieval_input.cutoff_at
    elif retrieval_input.temporal_mode is TemporalMode.PERSONAL_ARCHIVE:
        available_at_cutoff = retrieval_input.cutoff_at
        captured_at_cutoff = retrieval_input.cutoff_at

    return RetrievalFilters(
        document_ids=retrieval_input.document_ids,
        company_id=retrieval_input.company_id,
        source_attribution=retrieval_input.source_attribution,
        source_attribution_kind=retrieval_input.source_attribution_kind,
        published_from=retrieval_input.published_from,
        published_to=retrieval_input.published_to,
        available_at_cutoff=available_at_cutoff,
        captured_at_cutoff=captured_at_cutoff,
    )


class RetrieveResearchService:
    """Create one request and atomically persist its qualified retrieval result."""

    def __init__(
        self,
        *,
        unit_of_work_factory: ResearchRetrievalUnitOfWorkFactory,
        search_profile: LexicalSearchProfile,
        m1_query_retrieval: M1QueryRetrievalDependencies | None = None,
        query_planner: AccountedQueryPlanner | None = None,
        company_priority: CompanyPriorityReader | None = None,
        luna_reranker: AccountedLunaReranker | None = None,
        expand_text_neighbors: bool = False,
        expand_page_context: bool = False,
        tracer: OperationTracer | None = None,
        clock: Clock = lambda: datetime.now(UTC),
        identifier_factory: IdentifierFactory = uuid4,
    ) -> None:
        if (
            m1_query_retrieval is not None
            and isinstance(
                m1_query_retrieval.selection_profile, (RrfSelectionProfile, RrfRerankingProfile)
            )
            and max(
                search_profile.candidate_limit,
                m1_query_retrieval.dense_profile.candidate_limit,
            )
            > M1_LANE_CANDIDATE_LIMIT
        ):
            raise ValueError("RRF requires at most twenty candidates per lane")
        self._unit_of_work_factory = unit_of_work_factory
        self._search_profile = search_profile
        self._m1_query_retrieval = m1_query_retrieval
        self._query_planner = query_planner
        self._company_priority = company_priority
        self._luna_reranker = luna_reranker
        self._expand_text_neighbors = expand_text_neighbors
        self._expand_page_context = expand_page_context
        self._tracer = tracer
        self._clock = clock
        self._identifier_factory = identifier_factory

    @property
    def query_decomposition_enabled(self) -> bool:
        return self._m1_query_retrieval is not None and self._query_planner is not None

    @property
    def luna_reranking_enabled(self) -> bool:
        return (
            self._luna_reranker is not None
            and self._expand_page_context
            and self._m1_query_retrieval is not None
            and isinstance(self._m1_query_retrieval.selection_profile, RrfSelectionProfile)
        )

    def retrieve(
        self,
        context: WorkspaceContext,
        retrieval_input: ResearchRetrievalInput,
        *,
        progress: Callable[[UUID, ResearchProgressStage], None] | None = None,
    ) -> ResearchRetrievalResult:
        """Run the configured bounded retrieval mode and persist one coherent result."""

        mode = retrieval_input.effective_query_decomposition_mode
        if mode is QueryDecompositionMode.ALWAYS and not self.query_decomposition_enabled:
            raise ValueError("query decomposition is unavailable")
        if retrieval_input.rerank_with_luna and not self.luna_reranking_enabled:
            raise ValueError("Luna reranking is unavailable")
        started_at = self._aware_utc_now()
        request = ResearchRequest(
            id=self._identifier_factory(),
            workspace_id=context.workspace_id,
            question=retrieval_input.question,
            document_ids=retrieval_input.document_ids,
            temporal_mode=retrieval_input.temporal_mode,
            cutoff_at=retrieval_input.cutoff_at,
            created_at=started_at,
        )
        filters = build_effective_retrieval_filters(retrieval_input)
        query = build_lexical_search_query(
            query_text=request.question,
            filters=filters,
            lexical_config_version=self._search_profile.lexical_config_version,
            candidate_limit=self._search_profile.candidate_limit,
        )
        retrieval_run_id = self._identifier_factory()

        with trace_operation(
            self._tracer, "research.retrieve", attributes={"research.request.id": str(request.id)}
        ) as span:
            if self._m1_query_retrieval is not None:
                result = self._retrieve_dual_lane(
                    context=context,
                    request=request,
                    query=query,
                    retrieval_run_id=retrieval_run_id,
                    started_at=started_at,
                    progress=progress,
                    decomposition_mode=mode,
                    rerank_with_luna=retrieval_input.rerank_with_luna,
                    span=span,
                )
            else:
                result = self._retrieve_lexical(
                    context=context,
                    request=request,
                    query=query,
                    retrieval_run_id=retrieval_run_id,
                    started_at=started_at,
                    progress=progress,
                )
            if span is not None:
                span.set_attribute(
                    "configuration.version", result.retrieval_run.selector_config_version
                )
                span.set_attribute("chunk.count", len(result.sources))
            return result

    def _search_lexical(
        self,
        reader: LexicalSearch,
        context: WorkspaceContext,
        query: LexicalSearchQuery,
        additional_queries: tuple[str, ...] = (),
    ) -> tuple[LexicalSearchHit, ...]:
        with trace_operation(
            self._tracer,
            "postgresql.lexical_search",
            attributes={"configuration.version": query.lexical_config_version},
        ) as span:
            hits = search_lexical_queries(reader, context, query, additional_queries)
            if span is not None:
                span.set_attribute("candidate.count", len(hits))
            return hits

    def _retrieve_lexical(
        self,
        *,
        context: WorkspaceContext,
        request: ResearchRequest,
        query: LexicalSearchQuery,
        retrieval_run_id: UUID,
        started_at: datetime,
        progress: Callable[[UUID, ResearchProgressStage], None] | None,
    ) -> ResearchRetrievalResult:
        """Retain the credential-free lexical regression path."""

        with self._unit_of_work_factory() as unit_of_work:
            unit_of_work.research.add_request(context, request)
            if progress is not None:
                progress(request.id, "SEARCHING_SOURCES")
            hits = self._search_lexical(unit_of_work.lexical_search, context, query)
            if progress is not None:
                progress(request.id, "SELECTING_PASSAGES")
            completed_at = self._aware_utc_now()
            run = RetrievalRun(
                id=retrieval_run_id,
                workspace_id=context.workspace_id,
                research_request_id=request.id,
                query_text=request.question,
                normalized_query=query.normalized_query,
                filters=query.filters,
                mode=RetrievalMode.LEXICAL,
                lexical_config_version=query.lexical_config_version,
                selector_config_version=LEXICAL_ORDER_SELECTOR_CONFIG_VERSION,
                lexical_candidate_limit=query.candidate_limit,
                started_at=started_at,
                completed_at=completed_at,
            )
            candidates = tuple(
                RetrievalCandidate(
                    id=self._identifier_factory(),
                    workspace_id=context.workspace_id,
                    retrieval_run_id=run.id,
                    chunk_id=hit.chunk_id,
                    origin=RetrievalOrigin.LEXICAL,
                    rank=rank,
                    raw_score=hit.raw_score,
                    created_at=completed_at,
                )
                for rank, hit in enumerate(hits, start=1)
            )
            unit_of_work.retrieval.add_result(context, run, candidates)
            sources = tuple(
                CanonicalCandidateSource(
                    retrieval_candidate_id=candidate.id,
                    chunk_id=hit.chunk_id,
                    document_id=hit.document_id,
                    document_version_id=hit.document_version_id,
                    structure_block_id=hit.structure_block_id,
                    source_locator_kind=hit.source_locator_kind,
                    page_number=hit.page_number,
                    text=hit.text,
                )
                for candidate, hit in zip(candidates, hits, strict=True)
            )

        return ResearchRetrievalResult(
            request=request,
            retrieval_run=run,
            candidates=candidates,
            sources=sources,
        )

    def _automatic_decomposition_route(
        self,
        question: str,
        query: LexicalSearchQuery,
        selection_profile: M1SelectionProfile | RrfSelectionProfile | RrfRerankingProfile,
    ) -> Literal["standard", "quarter_followup", "planner"]:
        if (
            self._expand_page_context
            and isinstance(selection_profile, RrfSelectionProfile)
            and (query.filters.company_id is not None or self._company_priority is not None)
            and revenue_forecast_actual_request(question, comparison_quarters(question)) is not None
        ):
            return "quarter_followup"
        if self._query_planner is not None and has_explicit_multiple_search_goals(question):
            return "planner"
        return "standard"

    def _retrieve_dual_lane(
        self,
        *,
        context: WorkspaceContext,
        request: ResearchRequest,
        query: LexicalSearchQuery,
        retrieval_run_id: UUID,
        started_at: datetime,
        progress: Callable[[UUID, ResearchProgressStage], None] | None,
        decomposition_mode: QueryDecompositionMode,
        rerank_with_luna: bool,
        span: OperationSpan | None,
    ) -> ResearchRetrievalResult:
        """Run external query providers first, then commit canonical selection atomically."""
        dependencies = self._m1_query_retrieval
        if dependencies is None:
            raise RuntimeError("M1 query retrieval dependencies are required")

        with self._unit_of_work_factory() as unit_of_work:
            unit_of_work.research.add_request(context, request)
            document_version_ids = (
                unit_of_work.dense_qualifier.current_document_version_ids(
                    context, query.filters.document_ids
                )
                if query.filters.document_ids
                else None
            )

        route = "standard"
        if decomposition_mode is QueryDecompositionMode.AUTO:
            route = self._automatic_decomposition_route(
                request.question, query, dependencies.selection_profile
            )
        elif decomposition_mode is QueryDecompositionMode.ALWAYS:
            route = "planner"
        additional_queries = (
            self._query_planner.plan(context, request.id, request.question)
            if route == "planner" and self._query_planner is not None
            else ()
        )
        if span is not None and decomposition_mode is not QueryDecompositionMode.OFF:
            span.set_attribute("query.decomposition.mode", decomposition_mode.value)
            span.set_attribute("query.decomposition.route", route)
            span.set_attribute("query.decomposition.count", len(additional_queries))
        if progress is not None:
            progress(request.id, "EMBEDDING_QUESTION")
        query_embedding = AccountedQueryEmbedding(
            provider=dependencies.embedding_provider,
            executor=dependencies.provider_operation_executor,
            accounting=dependencies.embedding_accounting,
        )
        try:
            embedding_result = self._call_with_retry(
                lambda: query_embedding.embed(
                    context,
                    text=request.question,
                    configuration_version=dependencies.dense_profile.embedding_config_version,
                    correlation_id=request.id,
                    research_request_id=request.id,
                ),
                dependencies,
            )
        except ProviderBudgetExceededError as error:
            raise ResearchProviderUnavailableError(request.id, retriable=False) from error
        except ProviderUsageExceededReservationError as error:
            raise ResearchProviderUnavailableError(request.id, retriable=False) from error
        except SearchProviderError as error:
            raise ResearchProviderUnavailableError(
                request.id,
                retriable=isinstance(error, SearchProviderTransientError),
            ) from error
        embedding = embedding_result.vectors[0]
        dense_query = DenseRetrievalQuery(
            query_vector=embedding.values,
            filters=query.filters,
            embedding_config_version=dependencies.dense_profile.embedding_config_version,
            dense_index_config_version=dependencies.dense_profile.dense_index_config_version,
            candidate_limit=dependencies.dense_profile.candidate_limit,
            document_version_ids=document_version_ids,
        )
        if progress is not None:
            progress(request.id, "SEARCHING_SOURCES")
        try:
            with trace_operation(
                self._tracer,
                "pinecone.query",
                attributes={"configuration.version": dense_query.dense_index_config_version},
            ) as span:
                provider_hits = self._call_with_retry(
                    lambda: tuple(dependencies.dense_retriever.retrieve(context, dense_query)),
                    dependencies,
                )
                if span is not None:
                    span.set_attribute("candidate.count", len(provider_hits))
        except SearchProviderError as error:
            raise ResearchProviderUnavailableError(
                request.id,
                retriable=isinstance(error, SearchProviderTransientError),
            ) from error
        qualification_request = DenseQualificationRequest(
            provider_hits=provider_hits,
            filters=query.filters,
            lexical_config_version=query.lexical_config_version,
            embedding_config_version=dense_query.embedding_config_version,
            dense_index_config_version=dense_query.dense_index_config_version,
        )

        reranked: RrfRerankingSelectionResult | None = None
        lexical_snapshot: tuple[LexicalSearchHit, ...] = ()
        dense_snapshot: tuple[QualifiedDenseHit, ...] = ()
        if isinstance(dependencies.selection_profile, RrfRerankingProfile):
            with self._unit_of_work_factory() as unit_of_work:
                lexical_snapshot = self._search_lexical(
                    unit_of_work.lexical_search, context, query, additional_queries
                )
                dense_snapshot = tuple(
                    unit_of_work.dense_qualifier.qualify(context, qualification_request)
                )
            if progress is not None:
                progress(request.id, "SELECTING_PASSAGES")
            try:
                with trace_operation(self._tracer, "search.rrf") as span:
                    prepared = prepare_rrf_reranking(
                        normalized_query=query.normalized_query,
                        lexical_hits=lexical_snapshot,
                        dense_hits=dense_snapshot,
                        profile=dependencies.selection_profile,
                    )
                    if span is not None:
                        span.set_attribute("candidate.count", len(prepared.fused_candidates))
            except SearchProviderContractError as error:
                raise ResearchProviderUnavailableError(request.id, retriable=False) from error
            if dependencies.reranking is None:
                raise RuntimeError("accounted reranking service is required")
            # The detached snapshot and provider operation never retain this read transaction.
            reranked = dependencies.reranking.select(context, request.id, prepared)

        luna_selection: tuple[SelectedRetrievalCandidate, ...] | None = None
        baseline = None
        if rerank_with_luna:
            profile = dependencies.selection_profile
            assert isinstance(profile, RrfSelectionProfile) and self._luna_reranker is not None
            if progress is not None:
                progress(request.id, "SELECTING_PASSAGES")
            with self._unit_of_work_factory() as unit_of_work:
                lexical_snapshot = self._search_lexical(
                    unit_of_work.lexical_search, context, query, additional_queries
                )
                dense_snapshot = tuple(
                    unit_of_work.dense_qualifier.qualify(context, qualification_request)
                )
                baseline = self._select_expanded_context(
                    context,
                    query,
                    qualification_request,
                    request.question,
                    profile,
                    unit_of_work.lexical_search,
                    lexical_snapshot,
                    dense_snapshot,
                )
            selected, _, _, limit = baseline
            pool = {item.chunk_id: item for item in selected}
            for candidate in self._fuse_rrf_hits(
                lexical_hits=lexical_snapshot, dense_hits=dense_snapshot
            )[:20]:
                hit = candidate.hit
                origin = candidate.contributions[0]
                if hit.chunk_id not in pool and len(pool) < LUNA_RERANK_POOL_LIMIT:
                    pool[hit.chunk_id] = SelectedRetrievalCandidate(
                        **hit.model_dump(), origin=origin.origin, rank=origin.rank, selection_rank=1
                    )
            # Keep the experiment's original RRF input order and append structured additions.
            ranked_pool = tuple(
                sorted(pool.values(), key=lambda item: baseline[1].get(item.chunk_id, 41))
            )
            luna_selection = self._luna_reranker.select(
                context,
                request.id,
                request.question,
                ranked_pool,
                chunk_limit=limit,
                character_limit=profile.context_character_limit,
            )

        with self._unit_of_work_factory() as unit_of_work:
            lexical_hits = self._search_lexical(
                unit_of_work.lexical_search, context, query, additional_queries
            )
            dense_hits = tuple(unit_of_work.dense_qualifier.qualify(context, qualification_request))
            reranker_scores: dict[UUID, float] = {}
            structured_hits: tuple[LexicalSearchHit, ...] = ()
            selector_version = dependencies.selection_profile.selector_config_version
            chunk_limit = dependencies.selection_profile.context_chunk_limit
            if reranked is not None:
                # Reject any changed lane membership, order, provenance, text, or score.
                # Do not rerank again, substitute new sources, or reuse stale evidence.
                if lexical_hits != lexical_snapshot or dense_hits != dense_snapshot:
                    changed_sources_error = SearchProviderContractError(
                        "canonical retrieval changed during reranking"
                    )
                    raise ResearchProviderUnavailableError(
                        request.id, retriable=False
                    ) from changed_sources_error
                selected_candidates = tuple(
                    SelectedRetrievalCandidate(
                        **item.candidate.hit.model_dump(),
                        origin=item.candidate.contributions[0].origin,
                        rank=item.candidate.contributions[0].rank,
                        selection_rank=item.selection_rank,
                    )
                    for item in reranked.candidates
                )
                fused_ranks = {
                    item.hit.chunk_id: item.fused_rank
                    for item in reranked.prepared.fused_candidates
                }
                if reranked.response is not None:
                    reranker_scores = {
                        item.retrieval_candidate_id: item.score
                        for item in reranked.response.results
                    }
            else:
                if progress is not None and not rerank_with_luna:
                    progress(request.id, "SELECTING_PASSAGES")
                if isinstance(dependencies.selection_profile, RrfRerankingProfile):
                    raise RuntimeError("reranking selection was not prepared")
                current = self._select_expanded_context(
                    context,
                    query,
                    qualification_request,
                    request.question,
                    dependencies.selection_profile,
                    unit_of_work.lexical_search,
                    lexical_hits,
                    dense_hits,
                )
                selected_candidates, fused_ranks, selector_version, chunk_limit = current
                if luna_selection is not None:
                    if (
                        current != baseline
                        or lexical_hits != lexical_snapshot
                        or dense_hits != dense_snapshot
                    ):
                        raise ResearchProviderUnavailableError(request.id, retriable=False)
                    selected_candidates = luna_selection
                    selector_version = LUNA_RERANK_SELECTOR_VERSION
                selected_candidates, structured_hits = self._number_structured(selected_candidates)
            completed_at = self._aware_utc_now()
            run = RetrievalRun(
                id=retrieval_run_id,
                workspace_id=context.workspace_id,
                research_request_id=request.id,
                query_text=request.question,
                normalized_query=query.normalized_query,
                filters=query.filters,
                mode=RetrievalMode.DUAL_LANE,
                lexical_config_version=query.lexical_config_version,
                embedding_config_version=dense_query.embedding_config_version,
                dense_index_config_version=dense_query.dense_index_config_version,
                selector_config_version=selector_version,
                lexical_candidate_limit=query.candidate_limit,
                dense_candidate_limit=dense_query.candidate_limit,
                context_chunk_limit=chunk_limit,
                context_character_limit=dependencies.selection_profile.context_character_limit,
                started_at=started_at,
                completed_at=completed_at,
            )
            candidates, candidate_by_lane = self._build_dual_lane_candidates(
                context=context,
                run=run,
                lexical_hits=lexical_hits,
                dense_hits=dense_hits,
                neighbor_hits=structured_hits,
                selected_candidates=selected_candidates,
                fused_ranks=fused_ranks,
                reranker_scores=reranker_scores,
            )
            unit_of_work.retrieval.add_result(context, run, candidates)
            sources = tuple(
                CanonicalCandidateSource(
                    retrieval_candidate_id=candidate_by_lane[(selected.origin, selected.rank)].id,
                    chunk_id=selected.chunk_id,
                    document_id=selected.document_id,
                    document_version_id=selected.document_version_id,
                    structure_block_id=selected.structure_block_id,
                    source_locator_kind=selected.source_locator_kind,
                    page_number=selected.page_number,
                    text=selected.text,
                )
                for selected in selected_candidates
            )

        return ResearchRetrievalResult(
            request=request,
            retrieval_run=run,
            candidates=candidates,
            sources=sources,
        )

    def _select_expanded_context(
        self,
        context: WorkspaceContext,
        query: LexicalSearchQuery,
        qualification_request: DenseQualificationRequest,
        question: str,
        profile: M1SelectionProfile | RrfSelectionProfile,
        reader: LexicalSearch,
        lexical_hits: tuple[LexicalSearchHit, ...],
        dense_hits: tuple[QualifiedDenseHit, ...],
    ) -> tuple[tuple[SelectedRetrievalCandidate, ...], dict[UUID, int], str, int]:
        selector_version = profile.selector_config_version
        chunk_limit = profile.context_chunk_limit
        if (self._expand_text_neighbors or self._expand_page_context) and isinstance(
            profile, RrfSelectionProfile
        ):
            chunk_limit = min(profile.context_chunk_limit, TEXT_NEIGHBOR_CONTEXT_LIMIT)
            profile = profile.model_copy(update={"context_chunk_limit": chunk_limit})
            selector_version = (
                PAGE_SELECTOR_VERSION
                if self._expand_page_context
                else TEXT_NEIGHBOR_SELECTOR_VERSION
            )
        preferred_documents: frozenset[UUID] = frozenset()
        if (
            self._company_priority is not None
            and query.filters.company_id is None
            and selector_version == PAGE_SELECTOR_VERSION
        ):
            preferred_documents = self._company_priority.preferred_documents(
                context,
                question,
                {hit.document_id for hit in lexical_hits} | {hit.document_id for hit in dense_hits},
            )
        selected_candidates, fused_ranks = self._select_context(
            lexical_hits=lexical_hits,
            dense_hits=dense_hits,
            profile=profile,
            preferred_documents=preferred_documents,
        )
        if selector_version == PAGE_SELECTOR_VERSION:
            selected_candidates = self._expand_pages(
                context,
                query,
                qualification_request,
                question,
                profile,
                reader,
                selected_candidates,
            )
        if self._expand_text_neighbors and isinstance(profile, RrfSelectionProfile):
            anchors = tuple(
                item.chunk_id
                for item in selected_candidates[:TEXT_NEIGHBOR_ANCHOR_LIMIT]
                if item.source_locator_kind is SourceLocatorKind.TEXT_PASSAGE
            )
            if anchors:
                selected_candidates, _ = select_text_neighbors(
                    question,
                    selected_candidates,
                    reader.text_neighbors(
                        context,
                        query,
                        anchors,
                        embedding_config_version=qualification_request.embedding_config_version,
                        dense_index_config_version=qualification_request.dense_index_config_version,
                    ),
                    chunk_limit=profile.context_chunk_limit,
                    character_limit=profile.context_character_limit,
                )
        if selector_version == PAGE_SELECTOR_VERSION:
            pdf_anchors = tuple(
                item.chunk_id
                for item in selected_candidates[:PDF_NEIGHBOR_ANCHOR_LIMIT]
                if item.source_locator_kind is SourceLocatorKind.PDF_PAGE
            )
            if pdf_anchors:
                selected_candidates = add_pdf_neighbor(
                    question,
                    selected_candidates,
                    reader.pdf_neighbors(context, query, qualification_request, pdf_anchors),
                    chunk_limit=profile.context_chunk_limit,
                    character_limit=profile.context_character_limit,
                )
            expanded = self._expand_company_metrics(
                context,
                query,
                qualification_request,
                question,
                profile,
                reader,
                selected_candidates,
            )
            selected_candidates = (
                expanded
                if expanded is not None
                else self._expand_quarters(
                    context,
                    query,
                    qualification_request,
                    question,
                    profile,
                    reader,
                    selected_candidates,
                )
            )
        if preferred_documents:
            selector_version = COMPANY_PAGE_RRF_SELECTOR_VERSION
        return selected_candidates, fused_ranks, selector_version, chunk_limit

    def _expand_company_metrics(
        self,
        context: WorkspaceContext,
        query: LexicalSearchQuery,
        index: DenseQualificationRequest,
        question: str,
        profile: M1SelectionProfile | RrfSelectionProfile,
        reader: LexicalSearch,
        selected: tuple[SelectedRetrievalCandidate, ...],
    ) -> tuple[SelectedRetrievalCandidate, ...] | None:
        if (
            query.filters.company_id is not None
            or self._company_priority is None
            or profile.context_chunk_limit < 2
            or not (requests := company_metric_comparison(question))
        ):
            return None
        titles = reader.quarter_report_titles(context, query, index)
        seen_documents: set[UUID] = set()
        nominees: list[LexicalSearchHit] = []
        for request in requests:
            company_documents = self._company_priority.preferred_documents(
                context, request.company, titles.keys(), explicit_mention=True
            )
            if not company_documents or seen_documents & company_documents:
                return None
            seen_documents.update(company_documents)
            reports = frozenset(
                doc_id
                for doc_id in company_documents
                if (period := report_quarter(titles[doc_id])) is not None
                and request.quarter.matches(period)
            )
            if not 0 < len(reports) <= QUARTER_COMPARISON_DOCUMENT_LIMIT:
                return None
            passages, _ = reader.quarter_passages(context, query, index, reports)
            nominee = best_company_metric_passage(request.metric, passages)
            if nominee is None:
                return None
            nominees.append(nominee)
        return reserve_company_metric_passages(
            nominees,
            selected,
            chunk_limit=profile.context_chunk_limit,
            character_limit=profile.context_character_limit,
        )

    def _expand_quarters(
        self,
        context: WorkspaceContext,
        query: LexicalSearchQuery,
        index: DenseQualificationRequest,
        question: str,
        profile: M1SelectionProfile | RrfSelectionProfile,
        reader: LexicalSearch,
        selected: tuple[SelectedRetrievalCandidate, ...],
    ) -> tuple[SelectedRetrievalCandidate, ...]:
        quarters = comparison_quarters(question)
        if not quarters or (query.filters.company_id is None and self._company_priority is None):
            return selected
        titles = reader.quarter_report_titles(context, query, index)
        allowed: Set[UUID]
        if query.filters.company_id is None and self._company_priority is not None:
            company_question = re.sub(r"(?<=\w)[\u2019']s\b", "", question, flags=re.IGNORECASE)
            allowed = self._company_priority.preferred_documents(
                context, company_question, titles.keys()
            )
        else:
            allowed = titles.keys()
        reports: dict[UUID, ReportingQuarter] = {}
        for document_id in allowed:
            reported = report_quarter(titles[document_id])
            if reported is not None:
                for quarter in quarters:
                    if quarter.matches(reported):
                        reports[document_id] = quarter
                        break
        if (
            set(reports.values()) != set(quarters)
            or len(reports) > QUARTER_COMPARISON_DOCUMENT_LIMIT
        ):
            return selected
        passages, terms = reader.quarter_passages(context, query, index, frozenset(reports))
        bounds = {
            "chunk_limit": profile.context_chunk_limit,
            "character_limit": profile.context_character_limit,
        }
        expanded = add_quarter_context(
            question,
            quarters,
            reports,
            passages,
            terms,
            selected,
            **bounds,
        )
        nominees = revenue_forecast_actual_passages(question, quarters, reports, passages)
        return reserve_company_metric_passages(nominees, expanded, **bounds)

    @staticmethod
    def _expand_pages(
        context: WorkspaceContext,
        query: LexicalSearchQuery,
        index: DenseQualificationRequest,
        question: str,
        profile: M1SelectionProfile | RrfSelectionProfile,
        reader: LexicalSearch,
        selected: tuple[SelectedRetrievalCandidate, ...],
    ) -> tuple[SelectedRetrievalCandidate, ...]:
        bounds = {
            "chunk_limit": profile.context_chunk_limit,
            "character_limit": profile.context_character_limit,
        }
        # A single dated observation must earn new context through the exact-date lane.
        if not ends_with_observation_date(question):
            selected = add_page_context(
                selected, reader.search_pages(context, query, index), **bounds
            )
        metric = observation_metric(question)
        date = requested_observation_date(question)
        if metric is not None and date is not None:
            selected_pages = tuple(
                (item.document_version_id, item.page_number)
                for item in selected
                if item.page_number is not None
            )
            hit = reader.search_observation(context, query, index, metric, date, selected_pages)
            if hit is not None:
                selected = add_observation_context(selected, hit, **bounds)
        return selected

    @staticmethod
    def _number_structured(
        selected: tuple[SelectedRetrievalCandidate, ...],
    ) -> tuple[tuple[SelectedRetrievalCandidate, ...], tuple[LexicalSearchHit, ...]]:
        numbered: list[SelectedRetrievalCandidate] = []
        hits: list[LexicalSearchHit] = []
        for item in selected:
            if item.origin is RetrievalOrigin.STRUCTURED:
                hits.append(
                    LexicalSearchHit.model_validate(
                        item.model_dump(exclude={"origin", "rank", "selection_rank"})
                    )
                )
                item = item.model_copy(update={"rank": len(hits)})
            numbered.append(item)
        return tuple(numbered), tuple(hits)

    def _fuse_rrf_hits(
        self,
        *,
        lexical_hits: tuple[LexicalSearchHit, ...],
        dense_hits: tuple[QualifiedDenseHit, ...],
    ) -> tuple[RrfFusedCandidate, ...]:
        with trace_operation(self._tracer, "search.rrf") as span:
            candidates = fuse_rrf_hits(lexical_hits=lexical_hits, dense_hits=dense_hits)
            if span is not None:
                span.set_attribute("candidate.count", len(candidates))
            return candidates

    def _select_context(
        self,
        *,
        lexical_hits: tuple[LexicalSearchHit, ...],
        dense_hits: tuple[QualifiedDenseHit, ...],
        profile: M1SelectionProfile | RrfSelectionProfile,
        preferred_documents: Set[UUID] = frozenset(),
    ) -> tuple[tuple[SelectedRetrievalCandidate, ...], dict[UUID, int]]:
        """Normalize either policy to the existing canonical source contract."""

        if isinstance(profile, M1SelectionProfile):
            result = select_m1_context(
                lexical_hits=lexical_hits, dense_hits=dense_hits, profile=profile
            )
            return result.candidates, {}

        ranked = self._fuse_rrf_hits(lexical_hits=lexical_hits, dense_hits=dense_hits)
        selected = tuple(
            SelectedRetrievalCandidate(
                **item.candidate.hit.model_dump(),
                origin=item.candidate.contributions[0].origin,
                rank=item.candidate.contributions[0].rank,
                selection_rank=item.selection_rank,
            )
            for item in fit_rrf_context(ranked, profile, preferred_documents=preferred_documents)
        )
        return selected, {item.hit.chunk_id: item.fused_rank for item in ranked}

    def _build_dual_lane_candidates(
        self,
        *,
        context: WorkspaceContext,
        run: RetrievalRun,
        lexical_hits: tuple[LexicalSearchHit, ...],
        dense_hits: tuple[QualifiedDenseHit, ...],
        selected_candidates: tuple[SelectedRetrievalCandidate, ...],
        fused_ranks: dict[UUID, int],
        neighbor_hits: tuple[LexicalSearchHit, ...] = (),
        reranker_scores: dict[UUID, float] | None = None,
    ) -> tuple[
        tuple[RetrievalCandidate, ...],
        dict[tuple[RetrievalOrigin, int], RetrievalCandidate],
    ]:
        """Create all persisted lane candidates and attach final ranks only to selections."""

        selection_rank_by_lane = {
            (candidate.origin, candidate.rank): candidate.selection_rank
            for candidate in selected_candidates
        }
        candidates: list[RetrievalCandidate] = []
        candidate_by_lane: dict[tuple[RetrievalOrigin, int], RetrievalCandidate] = {}
        lanes: tuple[tuple[RetrievalOrigin, tuple[CanonicalRetrievalHit, ...]], ...] = (
            (RetrievalOrigin.LEXICAL, lexical_hits),
            (RetrievalOrigin.DENSE, dense_hits),
            (RetrievalOrigin.STRUCTURED, neighbor_hits),
        )
        for origin, hits in lanes:
            for rank, hit in enumerate(hits, start=1):
                candidate = RetrievalCandidate(
                    id=self._identifier_factory(),
                    workspace_id=context.workspace_id,
                    retrieval_run_id=run.id,
                    chunk_id=hit.chunk_id,
                    origin=origin,
                    rank=rank,
                    raw_score=hit.raw_score,
                    selection_rank=selection_rank_by_lane.get((origin, rank)),
                    fused_rank=(
                        None
                        if origin is RetrievalOrigin.STRUCTURED
                        else fused_ranks.get(hit.chunk_id)
                    ),
                    reranker_score=(reranker_scores or {}).get(hit.chunk_id),
                    created_at=run.completed_at,
                )
                candidates.append(candidate)
                candidate_by_lane[(origin, rank)] = candidate
        return tuple(candidates), candidate_by_lane

    @staticmethod
    def _call_with_retry[ResultT](
        operation: Callable[[], ResultT],
        dependencies: M1QueryRetrievalDependencies,
    ) -> ResultT:
        """Retry only classified transient provider failures under the frozen M1 policy."""

        for attempt in range(M1_PROVIDER_MAX_ATTEMPTS):
            try:
                return operation()
            except SearchProviderTransientError:
                if attempt + 1 == M1_PROVIDER_MAX_ATTEMPTS:
                    raise
                dependencies.sleeper(dependencies.retry_delay_seconds)
        raise RuntimeError("query provider retry loop terminated without a result")

    def _aware_utc_now(self) -> datetime:
        """Require an aware clock and normalize its instant before any write."""

        current_time = self._clock()
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise RuntimeError("research clock must return a timezone-aware datetime")
        return current_time.astimezone(UTC)
