"""Research retrieval orchestration and its M1 provider policy."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import sleep
from uuid import UUID, uuid4

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.application import (
    ExecuteProviderOperationService,
    ProviderBudgetExceededError,
    ProviderCostPolicy,
    ProviderOperationAttempt,
    ProviderUsageCostExceededReservationError,
)
from investment_research.modules.operations.domain import (
    ProviderOperationErrorCode,
    ProviderOperationKind,
    ProviderUsage,
)
from investment_research.modules.research.contracts import (
    CanonicalCandidateSource,
    ResearchProgressStage,
    ResearchRetrievalInput,
    ResearchRetrievalResult,
)
from investment_research.modules.research.domain import ResearchRequest, TemporalMode
from investment_research.modules.research.errors import ResearchProviderUnavailableError
from investment_research.modules.research.ports import ResearchRetrievalUnitOfWorkFactory
from investment_research.modules.search.application import (
    EmbeddingResponseContractError,
    build_lexical_search_query,
    validate_embedding_response,
)
from investment_research.modules.search.contracts import (
    M1_PROVIDER_MAX_ATTEMPTS,
    M1_PROVIDER_RETRY_DELAY_SECONDS,
    DenseQualificationRequest,
    DenseRetrievalProfile,
    DenseRetrievalQuery,
    EmbeddingInput,
    EmbeddingPurpose,
    EmbeddingRequest,
    LexicalSearchHit,
    LexicalSearchProfile,
    LexicalSearchQuery,
    M1SelectionProfile,
    M1SelectionResult,
    QualifiedDenseHit,
)
from investment_research.modules.search.domain import (
    LEXICAL_ORDER_SELECTOR_CONFIG_VERSION,
    RetrievalCandidate,
    RetrievalFilters,
    RetrievalMode,
    RetrievalOrigin,
    RetrievalRun,
)
from investment_research.modules.search.ports import (
    DenseRetriever,
    EmbeddingProvider,
    SearchProviderConfigurationError,
    SearchProviderContractError,
    SearchProviderError,
    SearchProviderTransientError,
)
from investment_research.modules.search.selection import select_m1_context

type Clock = Callable[[], datetime]
type IdentifierFactory = Callable[[], UUID]
type Sleeper = Callable[[float], None]
type CanonicalRetrievalHit = LexicalSearchHit | QualifiedDenseHit


@dataclass(frozen=True, slots=True)
class QueryEmbeddingAccountingProfile:
    """Provider identity and reviewed unit pricing for M1 query embeddings."""

    provider: str
    model: str
    cost_policy: ProviderCostPolicy


@dataclass(frozen=True, slots=True)
class M1QueryRetrievalDependencies:
    """Complete provider and policy bundle required by M1 dual-lane retrieval."""

    embedding_provider: EmbeddingProvider
    dense_retriever: DenseRetriever
    dense_profile: DenseRetrievalProfile
    selection_profile: M1SelectionProfile
    provider_operation_executor: ExecuteProviderOperationService
    embedding_accounting: QueryEmbeddingAccountingProfile
    retry_delay_seconds: float = M1_PROVIDER_RETRY_DELAY_SECONDS
    sleeper: Sleeper = sleep

    def __post_init__(self) -> None:
        """Keep injected retry timing inside the frozen M1 policy."""

        if not 0 <= self.retry_delay_seconds <= M1_PROVIDER_RETRY_DELAY_SECONDS:
            raise ValueError("query provider retry delay must be between 0 and 0.25 seconds")


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
        clock: Clock = lambda: datetime.now(UTC),
        identifier_factory: IdentifierFactory = uuid4,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._search_profile = search_profile
        self._m1_query_retrieval = m1_query_retrieval
        self._clock = clock
        self._identifier_factory = identifier_factory

    def retrieve(
        self,
        context: WorkspaceContext,
        retrieval_input: ResearchRetrievalInput,
        *,
        progress: Callable[[UUID, ResearchProgressStage], None] | None = None,
    ) -> ResearchRetrievalResult:
        """Run the configured bounded retrieval mode and persist one coherent result."""

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

        if self._m1_query_retrieval is not None:
            return self._retrieve_dual_lane(
                context=context,
                request=request,
                query=query,
                retrieval_run_id=retrieval_run_id,
                started_at=started_at,
                progress=progress,
            )

        return self._retrieve_lexical(
            context=context,
            request=request,
            query=query,
            retrieval_run_id=retrieval_run_id,
            started_at=started_at,
            progress=progress,
        )

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
            hits = tuple(unit_of_work.lexical_search.search(context, query))
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

    def _retrieve_dual_lane(
        self,
        *,
        context: WorkspaceContext,
        request: ResearchRequest,
        query: LexicalSearchQuery,
        retrieval_run_id: UUID,
        started_at: datetime,
        progress: Callable[[UUID, ResearchProgressStage], None] | None,
    ) -> ResearchRetrievalResult:
        """Run external query providers first, then commit canonical selection atomically."""
        dependencies = self._m1_query_retrieval
        if dependencies is None:
            raise RuntimeError("M1 query retrieval dependencies are required")

        with self._unit_of_work_factory() as unit_of_work:
            unit_of_work.research.add_request(context, request)

        if progress is not None:
            progress(request.id, "EMBEDDING_QUESTION")
        embedding_request = EmbeddingRequest(
            inputs=(
                EmbeddingInput(
                    input_id=request.id,
                    text=request.question,
                    purpose=EmbeddingPurpose.QUERY,
                ),
            ),
            embedding_config_version=dependencies.dense_profile.embedding_config_version,
        )
        embedding_attempt = ProviderOperationAttempt(
            correlation_id=request.id,
            research_request_id=request.id,
            processing_run_id=None,
            operation=ProviderOperationKind.QUERY_EMBEDDING,
            provider=dependencies.embedding_accounting.provider,
            model=dependencies.embedding_accounting.model,
            configuration_version=dependencies.dense_profile.embedding_config_version,
            maximum_usage=ProviderUsage(
                input_units=sum(
                    len(item.text.encode("utf-8")) for item in embedding_request.inputs
                ),
                output_units=0,
            ),
            cost_policy=dependencies.embedding_accounting.cost_policy,
        )
        try:
            embedding_result = self._call_with_retry(
                lambda: dependencies.provider_operation_executor.execute(
                    context,
                    embedding_attempt,
                    lambda: validate_embedding_response(
                        embedding_request,
                        dependencies.embedding_provider.embed(embedding_request),
                    ),
                    lambda result: result.usage,
                    self._classify_query_embedding_error,
                ),
                dependencies,
            )
        except ProviderBudgetExceededError as error:
            raise ResearchProviderUnavailableError(request.id, retriable=False) from error
        except ProviderUsageCostExceededReservationError as error:
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
        )
        if progress is not None:
            progress(request.id, "SEARCHING_SOURCES")
        try:
            provider_hits = self._call_with_retry(
                lambda: tuple(dependencies.dense_retriever.retrieve(context, dense_query)),
                dependencies,
            )
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

        with self._unit_of_work_factory() as unit_of_work:
            lexical_hits = tuple(unit_of_work.lexical_search.search(context, query))
            dense_hits = tuple(unit_of_work.dense_qualifier.qualify(context, qualification_request))
            if progress is not None:
                progress(request.id, "SELECTING_PASSAGES")
            selection = select_m1_context(
                lexical_hits=lexical_hits,
                dense_hits=dense_hits,
                profile=dependencies.selection_profile,
            )
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
                selector_config_version=selection.profile.selector_config_version,
                lexical_candidate_limit=query.candidate_limit,
                dense_candidate_limit=dense_query.candidate_limit,
                context_chunk_limit=selection.profile.context_chunk_limit,
                context_character_limit=selection.profile.context_character_limit,
                started_at=started_at,
                completed_at=completed_at,
            )
            candidates, candidate_by_lane = self._build_dual_lane_candidates(
                context=context,
                run=run,
                lexical_hits=lexical_hits,
                dense_hits=dense_hits,
                selection=selection,
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
                for selected in selection.candidates
            )

        return ResearchRetrievalResult(
            request=request,
            retrieval_run=run,
            candidates=candidates,
            sources=sources,
        )

    def _build_dual_lane_candidates(
        self,
        *,
        context: WorkspaceContext,
        run: RetrievalRun,
        lexical_hits: tuple[LexicalSearchHit, ...],
        dense_hits: tuple[QualifiedDenseHit, ...],
        selection: M1SelectionResult,
    ) -> tuple[
        tuple[RetrievalCandidate, ...],
        dict[tuple[RetrievalOrigin, int], RetrievalCandidate],
    ]:
        """Create all persisted lane candidates and attach final ranks only to selections."""

        selection_rank_by_lane = {
            (candidate.origin, candidate.rank): candidate.selection_rank
            for candidate in selection.candidates
        }
        candidates: list[RetrievalCandidate] = []
        candidate_by_lane: dict[tuple[RetrievalOrigin, int], RetrievalCandidate] = {}
        lanes: tuple[
            tuple[RetrievalOrigin, tuple[CanonicalRetrievalHit, ...]],
            tuple[RetrievalOrigin, tuple[CanonicalRetrievalHit, ...]],
        ] = (
            (RetrievalOrigin.LEXICAL, lexical_hits),
            (RetrievalOrigin.DENSE, dense_hits),
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

    @staticmethod
    def _classify_query_embedding_error(error: Exception) -> ProviderOperationErrorCode:
        """Map query-embedding failures to content-free canonical categories."""

        if isinstance(error, SearchProviderTransientError):
            return ProviderOperationErrorCode.UNAVAILABLE
        if isinstance(error, (SearchProviderContractError, EmbeddingResponseContractError)):
            return ProviderOperationErrorCode.CONTRACT
        if isinstance(error, SearchProviderConfigurationError):
            return ProviderOperationErrorCode.CONFIGURATION
        return ProviderOperationErrorCode.UNAVAILABLE

    def _aware_utc_now(self) -> datetime:
        """Require an aware clock and normalize its instant before any write."""

        current_time = self._clock()
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise RuntimeError("research clock must return a timezone-aware datetime")
        return current_time.astimezone(UTC)
