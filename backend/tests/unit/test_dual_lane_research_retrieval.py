"""Unit tests for M1 dual-lane research retrieval orchestration."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast
from unittest.mock import Mock, create_autospec
from uuid import UUID

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from investment_research.infrastructure.operations.opentelemetry import OpenTelemetryOperationTracer
from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.application import (
    ExecuteProviderOperationService,
    ProviderBudgetExceededError,
    ProviderCostPolicy,
    ProviderRequestQuotaUnavailableError,
    ProviderUsageCostExceededReservationError,
    ProviderUsageExceededReservationError,
)
from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.operations.ports import OperationTracer
from investment_research.modules.research.application import (
    M1QueryRetrievalDependencies,
    QueryEmbeddingAccountingProfile,
    ResearchProviderUnavailableError,
    RetrieveResearchService,
)
from investment_research.modules.research.company_priority import (
    CompanyPriorityReader,
    company_lookup_terms,
)
from investment_research.modules.research.contracts import (
    QueryDecompositionMode,
    ResearchRetrievalInput,
)
from investment_research.modules.research.luna_reranking import (
    AccountedLunaReranker,
    ListwiseRanking,
)
from investment_research.modules.research.ports import (
    ResearchRepository,
    ResearchRetrievalUnitOfWork,
    ResearchRetrievalUnitOfWorkFactory,
)
from investment_research.modules.research.query_planning import (
    AccountedQueryPlanner,
    QueryPlan,
    has_explicit_multiple_search_goals,
    search_lexical_queries,
)
from investment_research.modules.research.reranking import (
    AccountedRerankingService,
    RerankingAccountingProfile,
)
from investment_research.modules.search.application import build_lexical_search_query
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    DenseRetrievalHit,
    DenseRetrievalProfile,
    EmbeddingRequest,
    EmbeddingResult,
    EmbeddingVector,
    LexicalSearchHit,
    LexicalSearchProfile,
    M1SelectionProfile,
    QualifiedDenseHit,
    RerankRequest,
    RerankResponse,
    RerankResult,
)
from investment_research.modules.search.domain import (
    RetrievalFilters,
    RetrievalMode,
    RetrievalOrigin,
)
from investment_research.modules.search.fusion_contracts import (
    COMPANY_PAGE_RRF_SELECTOR_VERSION,
    LUNA_RERANK_SELECTOR_VERSION,
    RRF_CONFIG_VERSION,
    RrfSelectionProfile,
)
from investment_research.modules.search.page_context import PAGE_SELECTOR_VERSION, PdfNeighbor
from investment_research.modules.search.ports import (
    DenseCandidateQualifier,
    DenseRetriever,
    EmbeddingProvider,
    LexicalSearch,
    Reranker,
    RetrievalRepository,
    SearchProviderConfigurationError,
    SearchProviderContractError,
    SearchProviderError,
    SearchProviderTransientError,
)
from investment_research.modules.search.quarter_context import QuarterPassage
from investment_research.modules.search.reranking_contracts import (
    BGE_RERANK_CONFIG_VERSION,
    BGE_RERANK_MODEL,
    RrfRerankingProfile,
)
from investment_research.modules.search.text_neighbors import (
    TEXT_NEIGHBOR_SELECTOR_VERSION,
    TextNeighbor,
)

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
DOCUMENT_ID = UUID("10000000-0000-4000-8000-000000000001")
VERSION_ID = UUID("10000000-0000-4000-8000-000000000002")
BLOCK_ID = UUID("10000000-0000-4000-8000-000000000003")
FIRST_CHUNK_ID = UUID("10000000-0000-4000-8000-000000000004")
SECOND_CHUNK_ID = UUID("10000000-0000-4000-8000-000000000005")
REQUEST_ID = UUID("20000000-0000-4000-8000-000000000001")
RUN_ID = UUID("20000000-0000-4000-8000-000000000002")
LEXICAL_CANDIDATE_ID = UUID("20000000-0000-4000-8000-000000000003")
DENSE_DUPLICATE_CANDIDATE_ID = UUID("20000000-0000-4000-8000-000000000004")
DENSE_SELECTED_CANDIDATE_ID = UUID("20000000-0000-4000-8000-000000000005")
STARTED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 2, 20, 12, 0, 1, tzinfo=UTC)
EMBEDDING_CONFIG_VERSION = "openai-text-embedding-3-small-1536-v1"
DENSE_INDEX_CONFIG_VERSION = "pinecone-serverless-cosine-1536-aws-us-east-1-v1"
VECTOR_VALUES = (0.125,) * M1_EMBEDDING_DIMENSIONS


class RecordingUnitOfWork:
    """Expose recording database collaborators through one transaction boundary."""

    def __init__(
        self,
        *,
        lexical_hits: tuple[LexicalSearchHit, ...] = (),
        dense_hits: tuple[QualifiedDenseHit, ...] = (),
        events: list[str] | None = None,
    ) -> None:
        self.research_mock = create_autospec(ResearchRepository, instance=True)
        self.lexical_search_mock = create_autospec(LexicalSearch, instance=True)
        self.dense_qualifier_mock = create_autospec(DenseCandidateQualifier, instance=True)
        self.retrieval_mock = create_autospec(RetrievalRepository, instance=True)
        self.research = cast(ResearchRepository, self.research_mock)
        self.lexical_search = cast(LexicalSearch, self.lexical_search_mock)
        self.dense_qualifier = cast(DenseCandidateQualifier, self.dense_qualifier_mock)
        self.retrieval = cast(RetrievalRepository, self.retrieval_mock)
        self.lexical_search_mock.search.return_value = lexical_hits
        self.lexical_search_mock.pdf_neighbors.return_value = ()
        self.dense_qualifier_mock.qualify.return_value = dense_hits
        self.events = events if events is not None else []
        self.enter_count = 0
        self.active = False

    @contextmanager
    def open(self) -> Iterator[ResearchRetrievalUnitOfWork]:
        """Record when the short database transaction begins."""

        self.enter_count += 1
        self.events.append("database")
        self.active = True
        try:
            yield self
        finally:
            self.active = False


def make_context() -> WorkspaceContext:
    """Return the trusted identity used throughout the tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_lexical_hit() -> LexicalSearchHit:
    """Build the lexical route's canonical first-ranked hit."""

    return LexicalSearchHit(
        chunk_id=FIRST_CHUNK_ID,
        document_id=DOCUMENT_ID,
        document_version_id=VERSION_ID,
        structure_block_id=BLOCK_ID,
        source_locator_kind=SourceLocatorKind.PDF_PAGE,
        page_number=2,
        text="FY2025 revenue was $184.2 million.",
        raw_score=0.8,
    )


def test_decomposition_fuses_keyword_hits_under_the_same_scope_and_limit() -> None:
    reader = create_autospec(LexicalSearch, instance=True)
    first = make_lexical_hit()
    repeated = first.model_copy(update={"chunk_id": SECOND_CHUNK_ID})
    reader.search.side_effect = [(first,), (repeated,), (repeated,)]
    query = build_lexical_search_query(
        query_text="How did sales and profitability develop?",
        filters=RetrievalFilters(document_ids=(DOCUMENT_ID,), available_at_cutoff=STARTED_AT),
        lexical_config_version="m1-english-lexical-v1",
        candidate_limit=1,
    )

    assert search_lexical_queries(reader, make_context(), query, ("sales", "margin")) == (repeated,)
    searches = [call.args[1] for call in reader.search.call_args_list]
    assert [item.normalized_query for item in searches] == [
        query.normalized_query,
        "sales",
        "margin",
    ]
    assert all(item.filters == query.filters and item.candidate_limit == 1 for item in searches)


def test_query_planner_keeps_original_search_when_budget_reservation_fails() -> None:
    executor = make_provider_operation_executor()
    executor.execute.side_effect = ProviderBudgetExceededError
    provider = Mock(
        return_value=QueryPlan(("sales",), ProviderUsage(input_units=5, output_units=3))
    )
    planner = AccountedQueryPlanner(
        provider=provider,
        executor=executor,
        model="gpt-5.6-terra",
        cost_policy=ProviderCostPolicy(2_000_000, 12_000_000),
    )

    assert planner.plan(make_context(), REQUEST_ID, "What were sales?") == ()
    provider.assert_not_called()


def test_query_planner_preserves_the_requested_observation_date() -> None:
    planner = AccountedQueryPlanner(
        provider=Mock(
            return_value=QueryPlan(
                ("operating ARR November 5 2026",), ProviderUsage(input_units=5, output_units=3)
            )
        ),
        executor=make_provider_operation_executor(),
        model="gpt-5.6-terra",
        cost_policy=ProviderCostPolicy(2_000_000, 12_000_000),
    )

    assert planner.plan(
        make_context(), REQUEST_ID, "What was operating ARR for November 6, 2026?"
    ) == ("operating arr november 6 2026",)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What was revenue? Why did it grow?", True),
        ("What was revenue, and why did it grow?", True),
        ("Which sites are operating and which sites are planned?", True),
        ("What was Q3 revenue?", False),
        ("What did the company say about research and development?", False),
        ("What were revenue and gross margin?", False),
        ('What does the title "Who we are and what we build" mean?', False),
    ],
)
def test_auto_decomposition_requires_distinct_search_goals(question: str, expected: bool) -> None:
    assert has_explicit_multiple_search_goals(question) is expected


def test_legacy_decomposition_opt_in_cannot_conflict_with_auto() -> None:
    with pytest.raises(ValueError, match="decompose_query requires"):
        ResearchRetrievalInput(
            question="What was revenue?",
            decompose_query=True,
            query_decomposition_mode=QueryDecompositionMode.AUTO,
        )


def make_qualified_dense_hit(
    *,
    chunk_id: UUID,
    page_number: int,
    text: str,
    raw_score: float,
) -> QualifiedDenseHit:
    """Build one already canonically qualified dense route hit."""

    return QualifiedDenseHit(
        chunk_id=chunk_id,
        document_id=DOCUMENT_ID,
        document_version_id=VERSION_ID,
        structure_block_id=BLOCK_ID,
        source_locator_kind=SourceLocatorKind.PDF_PAGE,
        page_number=page_number,
        text=text,
        raw_score=raw_score,
    )


def make_provider_mocks(
    *,
    events: list[str] | None = None,
) -> tuple[Mock, Mock]:
    """Build deterministic query embedding and dense retrieval collaborators."""

    embedding_provider = create_autospec(EmbeddingProvider, instance=True)
    dense_retriever = create_autospec(DenseRetriever, instance=True)
    event_log = events if events is not None else []

    def embed(request: EmbeddingRequest) -> EmbeddingResult:
        event_log.append("embedding")
        input_id = request.inputs[0].input_id
        return EmbeddingResult(
            vectors=(
                EmbeddingVector(
                    input_id=input_id,
                    values=VECTOR_VALUES,
                    embedding_config_version=EMBEDDING_CONFIG_VERSION,
                ),
            ),
            usage=ProviderUsage(input_units=0, output_units=0),
        )

    def retrieve(*_: object) -> tuple[DenseRetrievalHit, ...]:
        event_log.append("pinecone")
        return (
            DenseRetrievalHit(chunk_id=FIRST_CHUNK_ID, raw_score=0.95),
            DenseRetrievalHit(chunk_id=SECOND_CHUNK_ID, raw_score=0.75),
        )

    embedding_provider.embed.side_effect = embed
    dense_retriever.retrieve.side_effect = retrieve
    return embedding_provider, dense_retriever


def make_provider_operation_executor() -> Mock:
    """Run provider callbacks while keeping accounting persistence outside retrieval tests."""

    executor = cast(Mock, create_autospec(ExecuteProviderOperationService, instance=True))
    executor.execute.side_effect = (
        lambda _context, _attempt, provider_call, _usage_from_result, _classify_error: (
            provider_call()
        )
    )
    return executor


def make_service(
    unit_of_work: RecordingUnitOfWork,
    *,
    embedding_provider: EmbeddingProvider,
    dense_retriever: DenseRetriever,
    identifiers: tuple[UUID, ...] = (REQUEST_ID, RUN_ID),
    sleeper: Mock | None = None,
    provider_operation_executor: Mock | None = None,
    selection_profile: M1SelectionProfile | RrfSelectionProfile | RrfRerankingProfile | None = None,
    lexical_limit: int = 10,
    dense_limit: int = 10,
    reranking: AccountedRerankingService | None = None,
    expand_text_neighbors: bool = False,
    expand_page_context: bool = False,
    company_priority: CompanyPriorityReader | None = None,
    luna_reranker: AccountedLunaReranker | None = None,
    query_planner: AccountedQueryPlanner | None = None,
    tracer: OperationTracer | None = None,
) -> RetrieveResearchService:
    """Build one deterministic dual-lane service."""

    identifier_values = iter(identifiers)
    clock_values = iter((STARTED_AT, COMPLETED_AT))
    unit_of_work_factory: ResearchRetrievalUnitOfWorkFactory = unit_of_work.open
    return RetrieveResearchService(
        unit_of_work_factory=unit_of_work_factory,
        search_profile=LexicalSearchProfile(
            lexical_config_version="m1-english-lexical-v1",
            candidate_limit=lexical_limit,
        ),
        m1_query_retrieval=M1QueryRetrievalDependencies(
            embedding_provider=embedding_provider,
            dense_retriever=dense_retriever,
            dense_profile=DenseRetrievalProfile(
                embedding_config_version=EMBEDDING_CONFIG_VERSION,
                dense_index_config_version=DENSE_INDEX_CONFIG_VERSION,
                candidate_limit=dense_limit,
            ),
            selection_profile=selection_profile or M1SelectionProfile(),
            provider_operation_executor=cast(
                ExecuteProviderOperationService,
                provider_operation_executor or make_provider_operation_executor(),
            ),
            embedding_accounting=QueryEmbeddingAccountingProfile(
                provider="openai",
                model="text-embedding-3-small",
                cost_policy=ProviderCostPolicy(
                    input_micro_usd_per_million_units=20_000,
                    output_micro_usd_per_million_units=0,
                ),
            ),
            retry_delay_seconds=0,
            sleeper=sleeper or Mock(),
            reranking=reranking,
        ),
        clock=clock_values.__next__,
        identifier_factory=identifier_values.__next__,
        expand_text_neighbors=expand_text_neighbors,
        expand_page_context=expand_page_context,
        company_priority=company_priority,
        luna_reranker=luna_reranker,
        query_planner=query_planner,
        tracer=tracer,
    )


@pytest.mark.parametrize(
    ("mode", "legacy_opt_in", "multiple_goals", "planner_available", "planner_calls"),
    [
        (QueryDecompositionMode.AUTO, False, True, True, 1),
        (QueryDecompositionMode.AUTO, False, False, True, 0),
        (QueryDecompositionMode.AUTO, False, True, False, 0),
        (QueryDecompositionMode.OFF, False, True, True, 0),
        (QueryDecompositionMode.ALWAYS, False, False, True, 1),
        (None, True, False, True, 1),
        (None, False, True, True, 0),
    ],
)
def test_dual_lane_retrieval_routes_the_existing_planner(
    mode: QueryDecompositionMode | None,
    legacy_opt_in: bool,
    multiple_goals: bool,
    planner_available: bool,
    planner_calls: int,
) -> None:
    unit_of_work = RecordingUnitOfWork()
    embedding, dense = make_provider_mocks()
    planner = create_autospec(AccountedQueryPlanner, instance=True)
    planner.plan.return_value = ("revenue growth",)
    service = make_service(
        unit_of_work,
        embedding_provider=embedding,
        dense_retriever=dense,
        query_planner=planner if planner_available else None,
    )
    question = "What was revenue, and why did it grow?" if multiple_goals else "What was revenue?"
    context = make_context()
    service.retrieve(
        context,
        ResearchRetrievalInput(
            question=question,
            company_id=DOCUMENT_ID,
            decompose_query=legacy_opt_in,
            query_decomposition_mode=mode,
        ),
    )

    assert planner.plan.call_count == planner_calls
    if planner_calls:
        planner.plan.assert_called_once_with(context, REQUEST_ID, question)
    embedding.embed.assert_called_once()
    assert embedding.embed.call_args.args[0].inputs[0].text == question
    dense.retrieve.assert_called_once()
    searches = [call.args[1] for call in unit_of_work.lexical_search_mock.search.call_args_list]
    assert len(searches) == 1 + planner_calls
    assert (
        searches[0].normalized_query
        == build_lexical_search_query(
            query_text=question,
            filters=searches[0].filters,
            lexical_config_version=searches[0].lexical_config_version,
            candidate_limit=searches[0].candidate_limit,
        ).normalized_query
    )
    assert all(query.filters.company_id == DOCUMENT_ID for query in searches)


def make_accounted_reranker(
    unit_of_work: RecordingUnitOfWork,
    executor: Mock | None = None,
) -> tuple[Mock, AccountedRerankingService]:
    reranker = create_autospec(Reranker, instance=True)

    def rerank(request: RerankRequest) -> RerankResponse:
        assert not unit_of_work.active
        return RerankResponse(
            results=tuple(
                RerankResult(
                    retrieval_candidate_id=item.retrieval_candidate_id, rank=rank, score=1 / rank
                )
                for rank, item in enumerate(reversed(request.candidates), 1)
            ),
            usage=ProviderUsage(input_units=1, output_units=0),
            model=BGE_RERANK_MODEL,
            reranker_config_version=BGE_RERANK_CONFIG_VERSION,
        )

    reranker.rerank.side_effect = rerank
    service = AccountedRerankingService(
        reranker=cast(Reranker, reranker),
        executor=cast(
            ExecuteProviderOperationService, executor or make_provider_operation_executor()
        ),
        accounting=RerankingAccountingProfile(
            provider="deterministic",
            cost_policy=ProviderCostPolicy(0, 0),
            maximum_usage=ProviderUsage(input_units=1, output_units=0),
        ),
        public_synthetic_content_approved=True,
    )
    return reranker, service


def test_reranking_runs_outside_transaction_revalidates_and_preserves_all_scores() -> None:
    lexical = make_lexical_hit()
    dense_hits = (
        QualifiedDenseHit.model_validate(lexical.model_dump() | {"raw_score": 0.95}),
        make_qualified_dense_hit(
            chunk_id=SECOND_CHUNK_ID, page_number=3, text="Second source.", raw_score=0.75
        ),
    )
    uow = RecordingUnitOfWork(lexical_hits=(lexical,), dense_hits=dense_hits)
    reranker, accounted = make_accounted_reranker(uow)
    embedding, dense = make_provider_mocks()
    service = make_service(
        uow,
        embedding_provider=embedding,
        dense_retriever=dense,
        selection_profile=RrfRerankingProfile(),
        reranking=accounted,
        identifiers=(
            REQUEST_ID,
            RUN_ID,
            LEXICAL_CANDIDATE_ID,
            DENSE_DUPLICATE_CANDIDATE_ID,
            DENSE_SELECTED_CANDIDATE_ID,
        ),
    )
    result = service.retrieve(make_context(), ResearchRetrievalInput(question="revenue"))
    assert uow.enter_count == 3
    assert (
        uow.lexical_search_mock.search.call_count
        == uow.dense_qualifier_mock.qualify.call_count
        == 2
    )
    reranker.rerank.assert_called_once()
    assert [source.chunk_id for source in result.sources] == [SECOND_CHUNK_ID, FIRST_CHUNK_ID]
    assert [item.fused_rank for item in result.candidates] == [1, 1, 2]
    assert [item.reranker_score for item in result.candidates] == [0.5, 0.5, 1.0]
    assert [item.selection_rank for item in result.candidates] == [2, None, 1]
    assert result.sources[0].retrieval_candidate_id == DENSE_SELECTED_CANDIDATE_ID
    assert result.sources[1].text == lexical.text


@pytest.mark.parametrize("lane", ["lexical", "dense"])
@pytest.mark.parametrize("change", ["missing", "text", "score", "version"])
def test_changed_canonical_snapshot_rejects_reranked_result(lane: str, change: str) -> None:
    lexical = make_lexical_hit()
    hit = lexical if lane == "lexical" else QualifiedDenseHit.model_validate(lexical.model_dump())
    uow = RecordingUnitOfWork()
    values = {"text": "Changed source.", "raw_score": 0.4, "document_version_id": UUID(int=999)}
    field = (
        "raw_score"
        if change == "score"
        else "document_version_id"
        if change == "version"
        else "text"
    )
    after = () if change == "missing" else (hit.model_copy(update={field: values[field]}),)
    target = (
        uow.lexical_search_mock.search if lane == "lexical" else uow.dense_qualifier_mock.qualify
    )
    target.side_effect = [(hit,), after]
    uow.dense_qualifier_mock.qualify.return_value = ()
    reranker, accounted = make_accounted_reranker(uow)
    embedding, dense = make_provider_mocks()
    service = make_service(
        uow,
        embedding_provider=embedding,
        dense_retriever=dense,
        selection_profile=RrfRerankingProfile(),
        reranking=accounted,
    )
    with pytest.raises(ResearchProviderUnavailableError) as captured:
        service.retrieve(make_context(), ResearchRetrievalInput(question="revenue"))
    assert captured.value.retriable is False
    assert isinstance(captured.value.__cause__, SearchProviderContractError)
    reranker.rerank.assert_called_once()
    uow.retrieval_mock.add_result.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        SearchProviderTransientError("safe"),
        SearchProviderConfigurationError("safe"),
        SearchProviderContractError("safe"),
    ],
)
def test_reranking_failure_has_no_automatic_retry_or_fallback(error: SearchProviderError) -> None:
    uow = RecordingUnitOfWork(lexical_hits=(make_lexical_hit(),))
    reranker, accounted = make_accounted_reranker(uow)
    reranker.rerank.side_effect = error
    embedding, dense = make_provider_mocks()
    sleeper = Mock()
    service = make_service(
        uow,
        embedding_provider=embedding,
        dense_retriever=dense,
        selection_profile=RrfRerankingProfile(),
        reranking=accounted,
        sleeper=sleeper,
    )
    with pytest.raises(ResearchProviderUnavailableError) as captured:
        service.retrieve(make_context(), ResearchRetrievalInput(question="revenue"))
    assert captured.value.retriable is isinstance(error, SearchProviderTransientError)
    reranker.rerank.assert_called_once()
    sleeper.assert_not_called()
    uow.retrieval_mock.add_result.assert_not_called()


@pytest.mark.parametrize(
    "error", [ProviderBudgetExceededError("budget"), ProviderRequestQuotaUnavailableError("quota")]
)
def test_reranking_reservation_rejection_prevents_provider_call(
    error: ProviderBudgetExceededError,
) -> None:
    uow = RecordingUnitOfWork(lexical_hits=(make_lexical_hit(),))
    executor = make_provider_operation_executor()
    executor.execute.side_effect = error
    reranker, accounted = make_accounted_reranker(uow, executor)
    embedding, dense = make_provider_mocks()
    service = make_service(
        uow,
        embedding_provider=embedding,
        dense_retriever=dense,
        selection_profile=RrfRerankingProfile(),
        reranking=accounted,
    )
    with pytest.raises(ResearchProviderUnavailableError) as captured:
        service.retrieve(make_context(), ResearchRetrievalInput(question="revenue"))
    assert not captured.value.retriable
    reranker.rerank.assert_not_called()
    uow.retrieval_mock.add_result.assert_not_called()


def test_empty_reranking_skips_provider_and_reservation() -> None:
    uow = RecordingUnitOfWork()
    executor = make_provider_operation_executor()
    reranker, accounted = make_accounted_reranker(uow, executor)
    embedding, dense = make_provider_mocks()
    service = make_service(
        uow,
        embedding_provider=embedding,
        dense_retriever=dense,
        selection_profile=RrfRerankingProfile(),
        reranking=accounted,
    )
    result = service.retrieve(make_context(), ResearchRetrievalInput(question="revenue"))
    assert result.sources == ()
    assert result.candidates == ()
    executor.execute.assert_not_called()
    reranker.rerank.assert_not_called()


def test_reranking_preflight_limit_rejects_before_reservation() -> None:
    uow = RecordingUnitOfWork(
        lexical_hits=(make_lexical_hit().model_copy(update={"text": "x" * 8_001}),)
    )
    executor = make_provider_operation_executor()
    reranker, accounted = make_accounted_reranker(uow, executor)
    embedding, dense = make_provider_mocks()
    service = make_service(
        uow,
        embedding_provider=embedding,
        dense_retriever=dense,
        selection_profile=RrfRerankingProfile(),
        reranking=accounted,
    )
    with pytest.raises(ResearchProviderUnavailableError):
        service.retrieve(make_context(), ResearchRetrievalInput(question="revenue"))
    executor.execute.assert_not_called()
    reranker.rerank.assert_not_called()
    uow.retrieval_mock.add_result.assert_not_called()


@pytest.mark.parametrize("with_profile", [False, True])
def test_reranking_policy_and_service_must_be_enabled_together(with_profile: bool) -> None:
    uow = RecordingUnitOfWork()
    _, accounted = make_accounted_reranker(uow)
    embedding, dense = make_provider_mocks()
    with pytest.raises(ValueError, match="configured together"):
        make_service(
            uow,
            embedding_provider=embedding,
            dense_retriever=dense,
            selection_profile=RrfRerankingProfile() if with_profile else M1SelectionProfile(),
            reranking=None if with_profile else accounted,
        )
    assert uow.enter_count == 0


def test_accounted_reranking_requires_explicit_data_approval_and_usage_ceiling() -> None:
    uow = RecordingUnitOfWork()
    reranker, accounted = make_accounted_reranker(uow)
    with pytest.raises(ValueError, match="content approval"):
        AccountedRerankingService(
            reranker=reranker,
            executor=accounted.executor,
            accounting=accounted.accounting,
            public_synthetic_content_approved=False,
        )
    for usage in (
        ProviderUsage(input_units=0, output_units=0),
        ProviderUsage(input_units=1, output_units=1),
    ):
        with pytest.raises(ValueError, match="input-unit ceiling"):
            RerankingAccountingProfile(
                provider="deterministic", cost_policy=ProviderCostPolicy(0, 0), maximum_usage=usage
            )


@pytest.mark.parametrize(("lexical_limit", "dense_limit"), [(21, 10), (10, 21)])
def test_rrf_service_rejects_oversized_lane_configuration_before_any_call(
    lexical_limit: int,
    dense_limit: int,
) -> None:
    unit_of_work = RecordingUnitOfWork()
    embedding, dense_provider = make_provider_mocks()
    with pytest.raises(ValueError, match="twenty candidates"):
        make_service(
            unit_of_work,
            embedding_provider=cast(EmbeddingProvider, embedding),
            dense_retriever=cast(DenseRetriever, dense_provider),
            selection_profile=RrfSelectionProfile(),
            lexical_limit=lexical_limit,
            dense_limit=dense_limit,
        )
    assert unit_of_work.enter_count == 0
    embedding.embed.assert_not_called()
    dense_provider.retrieve.assert_not_called()


@pytest.mark.parametrize("context_chunk_limit", [1, 2])
def test_rrf_retrieval_fuses_qualified_lanes_and_preserves_all_audit_rows(
    context_chunk_limit: int,
) -> None:
    lexical_hit = make_lexical_hit()
    second = lexical_hit.model_copy(
        update={
            "chunk_id": SECOND_CHUNK_ID,
            "text": "Operating margin increased.",
            "raw_score": 0.2,
        }
    )
    shared = QualifiedDenseHit.model_validate(second.model_dump() | {"raw_score": -0.9})
    unit_of_work = RecordingUnitOfWork(lexical_hits=(lexical_hit, second), dense_hits=(shared,))
    embedding, dense_provider = make_provider_mocks(events=unit_of_work.events)
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = OpenTelemetryOperationTracer(provider.get_tracer("test.retrieval"), provider=provider)
    service = make_service(
        unit_of_work,
        embedding_provider=cast(EmbeddingProvider, embedding),
        dense_retriever=cast(DenseRetriever, dense_provider),
        selection_profile=RrfSelectionProfile(context_chunk_limit=context_chunk_limit),
        tracer=tracer,
        identifiers=(
            REQUEST_ID,
            RUN_ID,
            LEXICAL_CANDIDATE_ID,
            DENSE_SELECTED_CANDIDATE_ID,
            DENSE_DUPLICATE_CANDIDATE_ID,
        ),
    )

    question = "revenue margin"
    try:
        with tracer.start_span("research.answer"):
            result = service.retrieve(make_context(), ResearchRetrievalInput(question=question))
        spans = {span.name: span for span in exporter.get_finished_spans()}
    finally:
        tracer.close()

    retrieval_span = spans["research.retrieve"]
    assert retrieval_span.parent == spans["research.answer"].context
    assert retrieval_span.attributes == {
        "research.request.id": str(REQUEST_ID),
        "configuration.version": RRF_CONFIG_VERSION,
        "chunk.count": context_chunk_limit,
    }
    for name in ("postgresql.lexical_search", "pinecone.query", "search.rrf"):
        span = spans[name]
        assert span.parent == retrieval_span.context
        assert span.attributes is not None and span.attributes["candidate.count"] == 2
    assert all(question not in span.to_json() for span in spans.values())
    assert all(lexical_hit.text not in span.to_json() for span in spans.values())

    assert result.retrieval_run.selector_config_version == RRF_CONFIG_VERSION
    assert [item.fused_rank for item in result.candidates] == [2, 1, 1]
    assert [item.selection_rank for item in result.candidates] == [
        2 if context_chunk_limit == 2 else None,
        1,
        None,
    ]
    assert [item.rank for item in result.candidates] == [1, 2, 1]
    assert [item.raw_score for item in result.candidates] == [0.8, 0.2, -0.9]
    assert result.sources[0].retrieval_candidate_id == DENSE_SELECTED_CANDIDATE_ID
    assert result.sources[0].chunk_id == SECOND_CHUNK_ID
    assert len(result.sources) == context_chunk_limit
    assert unit_of_work.events == ["database", "embedding", "pinecone", "database"]
    embedding.embed.assert_called_once()
    dense_provider.retrieve.assert_called_once()
    unit_of_work.dense_qualifier_mock.qualify.assert_called_once()
    unit_of_work.retrieval_mock.add_result.assert_called_once_with(
        make_context(), result.retrieval_run, result.candidates
    )


def test_rrf_retrieval_persists_fusion_rank_for_a_budget_excluded_chunk() -> None:
    oversized = make_lexical_hit()
    fitting = oversized.model_copy(update={"chunk_id": SECOND_CHUNK_ID, "text": "margin"})
    unit_of_work = RecordingUnitOfWork(lexical_hits=(oversized, fitting))
    embedding, dense_provider = make_provider_mocks()
    service = make_service(
        unit_of_work,
        embedding_provider=cast(EmbeddingProvider, embedding),
        dense_retriever=cast(DenseRetriever, dense_provider),
        selection_profile=RrfSelectionProfile(context_character_limit=6),
        identifiers=(REQUEST_ID, RUN_ID, LEXICAL_CANDIDATE_ID, DENSE_SELECTED_CANDIDATE_ID),
    )

    result = service.retrieve(make_context(), ResearchRetrievalInput(question="margin"))

    assert [item.fused_rank for item in result.candidates] == [1, 2]
    assert [item.selection_rank for item in result.candidates] == [None, 1]
    assert result.sources[0].text == "margin"
    # Raw dense hits exist, but none survived canonical qualification in this fixture.
    assert all(item.origin is RetrievalOrigin.LEXICAL for item in result.candidates)


def test_rrf_empty_qualified_lanes_preserve_an_explicit_empty_result() -> None:
    unit_of_work = RecordingUnitOfWork()
    embedding, dense_provider = make_provider_mocks()
    service = make_service(
        unit_of_work,
        embedding_provider=cast(EmbeddingProvider, embedding),
        dense_retriever=cast(DenseRetriever, dense_provider),
        selection_profile=RrfSelectionProfile(),
    )
    result = service.retrieve(make_context(), ResearchRetrievalInput(question="unknown"))
    assert result.retrieval_run.selector_config_version == RRF_CONFIG_VERSION
    assert result.candidates == ()
    assert result.sources == ()


def test_dual_lane_retrieval_persists_request_before_providers_and_result_afterward() -> None:
    events: list[str] = []
    embedding_mock, dense_mock = make_provider_mocks(events=events)
    lexical_hit = make_lexical_hit()
    dense_hits = (
        make_qualified_dense_hit(
            chunk_id=FIRST_CHUNK_ID,
            page_number=2,
            text=lexical_hit.text,
            raw_score=0.95,
        ),
        make_qualified_dense_hit(
            chunk_id=SECOND_CHUNK_ID,
            page_number=3,
            text="Operating margin increased to 18.4 percent.",
            raw_score=0.75,
        ),
    )
    unit_of_work = RecordingUnitOfWork(
        lexical_hits=(lexical_hit,),
        dense_hits=dense_hits,
        events=events,
    )
    unit_of_work.dense_qualifier_mock.current_document_version_ids.return_value = (VERSION_ID,)
    provider_operation_executor = make_provider_operation_executor()
    service = make_service(
        unit_of_work,
        embedding_provider=cast(EmbeddingProvider, embedding_mock),
        dense_retriever=cast(DenseRetriever, dense_mock),
        identifiers=(
            REQUEST_ID,
            RUN_ID,
            LEXICAL_CANDIDATE_ID,
            DENSE_DUPLICATE_CANDIDATE_ID,
            DENSE_SELECTED_CANDIDATE_ID,
        ),
        provider_operation_executor=provider_operation_executor,
    )

    result = service.retrieve(
        make_context(),
        ResearchRetrievalInput(
            question="How did sales and profitability develop?",
            document_ids=(DOCUMENT_ID,),
        ),
        progress=lambda request_id, stage: events.append(f"{request_id}:{stage}"),
    )

    assert events == [
        "database",
        f"{REQUEST_ID}:EMBEDDING_QUESTION",
        "embedding",
        f"{REQUEST_ID}:SEARCHING_SOURCES",
        "pinecone",
        "database",
        f"{REQUEST_ID}:SELECTING_PASSAGES",
    ]
    assert unit_of_work.enter_count == 2
    assert result.retrieval_run.mode is RetrievalMode.DUAL_LANE
    assert result.retrieval_run.embedding_config_version == EMBEDDING_CONFIG_VERSION
    assert result.retrieval_run.dense_index_config_version == DENSE_INDEX_CONFIG_VERSION
    assert [(candidate.origin, candidate.rank) for candidate in result.candidates] == [
        (RetrievalOrigin.LEXICAL, 1),
        (RetrievalOrigin.DENSE, 1),
        (RetrievalOrigin.DENSE, 2),
    ]
    assert [candidate.selection_rank for candidate in result.candidates] == [1, None, 2]
    assert [source.chunk_id for source in result.sources] == [
        FIRST_CHUNK_ID,
        SECOND_CHUNK_ID,
    ]
    assert [source.retrieval_candidate_id for source in result.sources] == [
        LEXICAL_CANDIDATE_ID,
        DENSE_SELECTED_CANDIDATE_ID,
    ]
    unit_of_work.retrieval_mock.add_result.assert_called_once_with(
        make_context(),
        result.retrieval_run,
        result.candidates,
    )
    unit_of_work.research_mock.add_request.assert_called_once_with(
        make_context(),
        result.request,
    )

    embedding_request = embedding_mock.embed.call_args.args[0]
    assert embedding_request.inputs[0].input_id == REQUEST_ID
    assert embedding_request.inputs[0].purpose.value == "QUERY"
    assert embedding_request.inputs[0].text == "How did sales and profitability develop?"
    accounting_context, accounting_attempt = provider_operation_executor.execute.call_args.args[:2]
    assert accounting_context == make_context()
    assert accounting_attempt.correlation_id == REQUEST_ID
    assert accounting_attempt.research_request_id == REQUEST_ID
    assert accounting_attempt.operation.value == "QUERY_EMBEDDING"
    assert accounting_attempt.maximum_usage == ProviderUsage(
        input_units=len(b"How did sales and profitability develop?"),
        output_units=0,
    )
    dense_context, dense_query = dense_mock.retrieve.call_args.args
    assert dense_context == make_context()
    assert dense_query.filters.document_ids == (DOCUMENT_ID,)
    assert dense_query.document_version_ids == (VERSION_ID,)
    unit_of_work.dense_qualifier_mock.current_document_version_ids.assert_called_once_with(
        make_context(), (DOCUMENT_ID,)
    )
    qualification_context, qualification_request = (
        unit_of_work.dense_qualifier_mock.qualify.call_args.args
    )
    assert qualification_context == make_context()
    assert qualification_request.provider_hits == (
        DenseRetrievalHit(chunk_id=FIRST_CHUNK_ID, raw_score=0.95),
        DenseRetrievalHit(chunk_id=SECOND_CHUNK_ID, raw_score=0.75),
    )


def test_dual_lane_retrieval_preserves_explicit_empty_evidence() -> None:
    embedding_mock, dense_mock = make_provider_mocks()
    dense_mock.retrieve.side_effect = None
    dense_mock.retrieve.return_value = ()
    unit_of_work = RecordingUnitOfWork()
    service = make_service(
        unit_of_work,
        embedding_provider=cast(EmbeddingProvider, embedding_mock),
        dense_retriever=cast(DenseRetriever, dense_mock),
    )

    result = service.retrieve(
        make_context(),
        ResearchRetrievalInput(question="What dividend was declared?"),
    )

    assert result.retrieval_run.mode is RetrievalMode.DUAL_LANE
    assert result.candidates == ()
    assert result.sources == ()


@pytest.mark.parametrize("operation", ["embedding", "dense"])
def test_dual_lane_retries_one_transient_provider_failure(operation: str) -> None:
    embedding_mock, dense_mock = make_provider_mocks()
    embedding_vector = EmbeddingVector(
        input_id=REQUEST_ID,
        values=VECTOR_VALUES,
        embedding_config_version=EMBEDDING_CONFIG_VERSION,
    )
    embedding_result = EmbeddingResult(
        vectors=(embedding_vector,),
        usage=ProviderUsage(input_units=0, output_units=0),
    )
    if operation == "embedding":
        embedding_mock.embed.side_effect = [
            SearchProviderTransientError("temporary"),
            embedding_result,
        ]
        dense_mock.retrieve.side_effect = None
        dense_mock.retrieve.return_value = ()
    else:
        embedding_mock.embed.side_effect = None
        embedding_mock.embed.return_value = embedding_result
        dense_mock.retrieve.side_effect = [SearchProviderTransientError("temporary"), ()]
    sleeper = Mock()
    unit_of_work = RecordingUnitOfWork()
    service = make_service(
        unit_of_work,
        embedding_provider=cast(EmbeddingProvider, embedding_mock),
        dense_retriever=cast(DenseRetriever, dense_mock),
        sleeper=sleeper,
    )

    service.retrieve(
        make_context(),
        ResearchRetrievalInput(question="What was FY2025 revenue?"),
    )

    retried_mock = embedding_mock.embed if operation == "embedding" else dense_mock.retrieve
    assert retried_mock.call_count == 2
    sleeper.assert_called_once_with(0)
    assert unit_of_work.enter_count == 2


@pytest.mark.parametrize(
    "provider_error",
    [
        pytest.param(SearchProviderConfigurationError("permanent"), id="permanent"),
        pytest.param(SearchProviderTransientError("temporary"), id="transient-exhausted"),
    ],
)
def test_provider_failure_does_not_open_database_transaction(
    provider_error: Exception,
) -> None:
    embedding_mock, dense_mock = make_provider_mocks()
    if isinstance(provider_error, SearchProviderTransientError):
        embedding_mock.embed.side_effect = [provider_error, provider_error]
    else:
        embedding_mock.embed.side_effect = provider_error
    unit_of_work = RecordingUnitOfWork()
    service = make_service(
        unit_of_work,
        embedding_provider=cast(EmbeddingProvider, embedding_mock),
        dense_retriever=cast(DenseRetriever, dense_mock),
    )

    with pytest.raises(ResearchProviderUnavailableError) as captured:
        service.retrieve(
            make_context(),
            ResearchRetrievalInput(question="What was FY2025 revenue?"),
        )

    assert captured.value.research_request_id == REQUEST_ID
    assert captured.value.retriable is isinstance(provider_error, SearchProviderTransientError)
    assert captured.value.__cause__ is provider_error
    assert unit_of_work.enter_count == 1
    unit_of_work.research_mock.add_request.assert_called_once()
    unit_of_work.retrieval_mock.add_result.assert_not_called()


def test_budget_rejection_keeps_persisted_request_and_is_not_retriable() -> None:
    """Expose the frozen safe provider error without contacting embedding or retrieval providers."""

    embedding_mock, dense_mock = make_provider_mocks()
    executor = make_provider_operation_executor()
    executor.execute.side_effect = ProviderBudgetExceededError("monthly provider budget exceeded")
    unit_of_work = RecordingUnitOfWork()
    service = make_service(
        unit_of_work,
        embedding_provider=cast(EmbeddingProvider, embedding_mock),
        dense_retriever=cast(DenseRetriever, dense_mock),
        provider_operation_executor=executor,
    )

    with pytest.raises(ResearchProviderUnavailableError) as captured:
        service.retrieve(
            make_context(),
            ResearchRetrievalInput(question="What was FY2025 revenue?"),
        )

    assert captured.value.research_request_id == REQUEST_ID
    assert captured.value.retriable is False
    assert isinstance(captured.value.__cause__, ProviderBudgetExceededError)
    assert unit_of_work.enter_count == 1
    embedding_mock.embed.assert_not_called()
    dense_mock.retrieve.assert_not_called()
    unit_of_work.retrieval_mock.add_result.assert_not_called()


@pytest.mark.parametrize(
    "usage_error",
    [
        ProviderUsageCostExceededReservationError("provider usage cost exceeded its reservation"),
        ProviderUsageExceededReservationError("provider usage units exceeded their maximum"),
    ],
)
def test_usage_above_reservation_is_not_retriable_and_stops_before_dense_search(
    usage_error: ProviderUsageExceededReservationError,
) -> None:
    embedding_mock, dense_mock = make_provider_mocks()
    executor = make_provider_operation_executor()

    def fail_after_provider(
        _context: object,
        _attempt: object,
        provider_call: Callable[[], object],
        _usage_from_result: object,
        _classify_error: object,
    ) -> object:
        provider_call()
        raise usage_error

    executor.execute.side_effect = fail_after_provider
    unit_of_work = RecordingUnitOfWork()
    service = make_service(
        unit_of_work,
        embedding_provider=cast(EmbeddingProvider, embedding_mock),
        dense_retriever=cast(DenseRetriever, dense_mock),
        provider_operation_executor=executor,
    )

    with pytest.raises(ResearchProviderUnavailableError) as captured:
        service.retrieve(
            make_context(),
            ResearchRetrievalInput(question="What was FY2025 revenue?"),
        )

    assert captured.value.retriable is False
    assert captured.value.__cause__ is usage_error
    embedding_mock.embed.assert_called_once()
    dense_mock.retrieve.assert_not_called()
    unit_of_work.retrieval_mock.add_result.assert_not_called()


def test_query_dependencies_reject_retry_delay_above_frozen_policy() -> None:
    embedding_mock, dense_mock = make_provider_mocks()

    with pytest.raises(ValueError, match=r"between 0 and 0\.25"):
        M1QueryRetrievalDependencies(
            embedding_provider=cast(EmbeddingProvider, embedding_mock),
            dense_retriever=cast(DenseRetriever, dense_mock),
            dense_profile=DenseRetrievalProfile(
                embedding_config_version=EMBEDDING_CONFIG_VERSION,
                dense_index_config_version=DENSE_INDEX_CONFIG_VERSION,
                candidate_limit=10,
            ),
            selection_profile=M1SelectionProfile(),
            provider_operation_executor=cast(
                ExecuteProviderOperationService,
                make_provider_operation_executor(),
            ),
            embedding_accounting=QueryEmbeddingAccountingProfile(
                provider="openai",
                model="text-embedding-3-small",
                cost_policy=ProviderCostPolicy(
                    input_micro_usd_per_million_units=20_000,
                    output_micro_usd_per_million_units=0,
                ),
            ),
            retry_delay_seconds=0.5,
        )


@pytest.mark.parametrize(
    ("question", "expected_count"),
    [
        ("How does the technology reduce idle GPU time?", 3),
        ("Why was idle GPU time higher for November 6, 2026?", 1),
        ("Which platform uses the technology?", 1),
    ],
)
def test_rrf_text_neighbors_keep_fusion_and_enforce_observation_scope(
    question: str, expected_count: int
) -> None:
    anchor = make_lexical_hit().model_copy(
        update={
            "source_locator_kind": SourceLocatorKind.TEXT_PASSAGE,
            "page_number": None,
            "text": "The technology joined the platform.",
        }
    )
    neighbor_hits = tuple(
        anchor.model_copy(
            update={
                "chunk_id": UUID(int=100 + index),
                "structure_block_id": UUID(int=200 + index),
                "text": text,
            }
        )
        for index, text in enumerate(
            ("GPUs sit idle during cold starts.", "The technology reduces idle GPU time.")
        )
    )
    unit_of_work = RecordingUnitOfWork(lexical_hits=(anchor,))
    unit_of_work.lexical_search_mock.text_neighbors.return_value = tuple(
        TextNeighbor(
            anchor_id=anchor.chunk_id,
            offset=offset,
            hit=hit,
            anchor_terms=frozenset({"technolog", "platform"}),
            neighbor_terms=frozenset({"gpus" if offset == 1 else "gpu", "idl"}),
            query_terms=(
                frozenset({"technolog", "platform"})
                if question.startswith("Which")
                else frozenset({"gpu", "idl", "time"})
            ),
        )
        for offset, hit in enumerate(neighbor_hits, 1)
    )
    embedding, dense = make_provider_mocks()
    service = make_service(
        unit_of_work,
        embedding_provider=embedding,
        dense_retriever=dense,
        identifiers=(REQUEST_ID, RUN_ID, *(UUID(int=400 + index) for index in range(3))),
        selection_profile=RrfSelectionProfile(context_chunk_limit=16),
        expand_text_neighbors=True,
    )

    result = service.retrieve(make_context(), ResearchRetrievalInput(question=question))

    assert result.retrieval_run.selector_config_version == TEXT_NEIGHBOR_SELECTOR_VERSION
    assert result.retrieval_run.context_chunk_limit == 8
    assert len(result.sources) == expected_count
    assert result.candidates[0].fused_rank == 1
    if expected_count == 3:
        assert [item.chunk_id for item in result.sources] == [
            anchor.chunk_id,
            *(item.chunk_id for item in neighbor_hits),
        ]
        assert all(
            item.origin is RetrievalOrigin.STRUCTURED and item.fused_rank is None
            for item in result.candidates[1:]
        )


@pytest.mark.parametrize(
    ("neighbor_page", "observation_date", "expect_neighbor"),
    [(2, "", True), (3, "", False), (2, " as of November 6, 2026", False)],
)
def test_rrf_pdf_neighbor_adds_missing_sales_within_the_context_budget(
    neighbor_page: int, observation_date: str, expect_neighbor: bool
) -> None:
    anchor = make_lexical_hit().model_copy(
        update={"text": "Gross margin was 12.7% versus 17.5% in the prior quarter."}
    )
    sales = anchor.model_copy(
        update={
            "chunk_id": UUID(int=500),
            "structure_block_id": UUID(int=501),
            "page_number": neighbor_page,
            "text": "Net sales were $19.9 billion versus $11.1 billion in the prior quarter.",
        }
    )
    other_hits = tuple(
        anchor.model_copy(
            update={
                "chunk_id": UUID(int=510 + offset),
                "structure_block_id": UUID(int=520 + offset),
                "document_id": UUID(int=530 + offset),
                "document_version_id": UUID(int=540 + offset),
                "text": f"Unrelated selected passage {offset}.",
            }
        )
        for offset in range(7)
    )
    initial = (anchor, *other_hits)
    uow = RecordingUnitOfWork(lexical_hits=initial)
    uow.lexical_search_mock.pdf_neighbors.return_value = (PdfNeighbor(anchor.chunk_id, sales),)
    embedder, dense = make_provider_mocks()
    service = make_service(
        uow,
        embedding_provider=embedder,
        dense_retriever=dense,
        identifiers=(REQUEST_ID, RUN_ID, *(UUID(int=550 + index) for index in range(9))),
        selection_profile=RrfSelectionProfile(context_chunk_limit=16),
        expand_page_context=True,
    )

    result = service.retrieve(
        make_context(),
        ResearchRetrievalInput(
            question=f"How did net sales and gross margin change{observation_date}?"
        ),
    )

    assert result.retrieval_run.selector_config_version == PAGE_SELECTOR_VERSION
    assert len(result.sources) == 8
    assert uow.lexical_search_mock.pdf_neighbors.call_args.args[3] == tuple(
        item.chunk_id for item in initial[:4]
    )
    if expect_neighbor:
        expected = (anchor, sales, *initial[1:-1])
        structured = [
            item for item in result.candidates if item.origin is RetrievalOrigin.STRUCTURED
        ]
        assert len(structured) == 1
        assert structured[0].chunk_id == sales.chunk_id
        assert structured[0].fused_rank is None
    else:
        expected = initial
    assert [item.chunk_id for item in result.sources] == [item.chunk_id for item in expected]


def test_quarter_followup_respects_manual_company_scope() -> None:
    q2 = make_lexical_hit().model_copy(update={"text": "Operating margin reached 12%."})
    q3 = q2.model_copy(
        update={
            "chunk_id": UUID(int=650),
            "document_id": UUID(int=651),
            "document_version_id": UUID(int=652),
            "structure_block_id": UUID(int=653),
            "text": "Operating margin reached 15%.",
        }
    )
    company_id = UUID(int=654)
    uow = RecordingUnitOfWork(lexical_hits=(q2,))
    uow.lexical_search_mock.quarter_report_titles.return_value = {
        q2.document_id: "Example Q2 2026 earnings release",
        q3.document_id: "Example Q3 2026 earnings release",
    }
    terms = frozenset({"oper", "margin"})
    uow.lexical_search_mock.quarter_passages.return_value = (
        (QuarterPassage(q2, terms), QuarterPassage(q3, terms)),
        terms,
    )
    embedder, dense = make_provider_mocks()
    service = make_service(
        uow,
        embedding_provider=embedder,
        dense_retriever=dense,
        identifiers=(REQUEST_ID, RUN_ID, UUID(int=655), UUID(int=656)),
        selection_profile=RrfSelectionProfile(),
        expand_page_context=True,
    )

    result = service.retrieve(
        make_context(),
        ResearchRetrievalInput(
            question="How did operating margin change from Q2 to Q3 2026?",
            company_id=company_id,
        ),
    )

    _, query, qualified, documents = uow.lexical_search_mock.quarter_passages.call_args.args
    assert query.filters.company_id == qualified.filters.company_id == company_id
    assert documents == frozenset({q2.document_id, q3.document_id})
    assert [item.chunk_id for item in result.sources] == [q2.chunk_id, q3.chunk_id]
    assert result.candidates[-1].origin is RetrievalOrigin.STRUCTURED


def test_quarter_followup_reserves_forecast_and_reported_revenue_at_the_context_limit() -> None:
    outlook = make_lexical_hit().model_copy(
        update={"text": "Full-year 2027 revenue guidance is $6.1 billion to $6.3 billion."}
    )
    actual = outlook.model_copy(
        update={
            "chunk_id": UUID(int=635),
            "document_id": UUID(int=636),
            "document_version_id": UUID(int=637),
            "structure_block_id": UUID(int=638),
            "text": "YoYQ3'26Q3'27$ in millions 17%$700.0 $819.0Revenue",
        }
    )
    others = tuple(
        outlook.model_copy(
            update={
                "chunk_id": UUID(int=700 + index),
                "structure_block_id": UUID(int=710 + index),
                "text": f"An industry update about market conditions, part {index}.",
            }
        )
        for index in range(7)
    )
    uow = RecordingUnitOfWork(lexical_hits=(outlook, *others))
    uow.lexical_search_mock.search_pages.return_value = ()
    uow.lexical_search_mock.quarter_report_titles.return_value = {
        outlook.document_id: "Cedar Q2 2027 earnings release",
        actual.document_id: "Cedar Q3 2027 supplemental figures",
    }
    terms = frozenset({"revenu", "guidanc"})
    uow.lexical_search_mock.quarter_passages.return_value = (
        (QuarterPassage(outlook, terms), QuarterPassage(actual, terms)),
        terms,
    )
    embedder, dense = make_provider_mocks()
    service = make_service(
        uow,
        embedding_provider=embedder,
        dense_retriever=dense,
        identifiers=(REQUEST_ID, RUN_ID, *(UUID(int=730 + index) for index in range(9))),
        selection_profile=RrfSelectionProfile(context_chunk_limit=8),
        expand_page_context=True,
    )

    result = service.retrieve(
        make_context(),
        ResearchRetrievalInput(
            question="What full-year 2027 revenue range did Cedar guide to in Q2, "
            "and what Q3 revenue did it subsequently report?",
            company_id=UUID(int=739),
        ),
    )

    assert len(result.sources) == 8
    assert len({item.chunk_id for item in result.sources}) == 8
    assert [item.chunk_id for item in result.sources[:2]] == [outlook.chunk_id, actual.chunk_id]
    assert result.candidates[-1].origin is RetrievalOrigin.STRUCTURED
    embedder.embed.assert_called_once()
    dense.retrieve.assert_called_once()


@pytest.mark.parametrize("mode", ["comparison", "filtered", "cross_attribution", "ambiguous"])
def test_company_metric_followup_reserves_actual_evidence(mode: str) -> None:
    anchor = make_lexical_hit()
    actual = anchor.model_copy(
        update={
            "text": "Our AI server business recognized $16.4 billion in revenue.",
            "chunk_id": UUID(int=660),
            "structure_block_id": UUID(int=661),
        }
    )
    forecast = anchor.model_copy(
        update={"text": "We expect AI server revenue of $74 billion.", "chunk_id": UUID(int=662)}
    )
    other = anchor.model_copy(
        update={
            "text": "Net sales of $19.9 billion.",
            "chunk_id": UUID(int=663),
            "document_id": UUID(int=664),
            "document_version_id": UUID(int=665),
            "structure_block_id": UUID(int=666),
        }
    )
    uow = RecordingUnitOfWork(lexical_hits=(anchor,))
    uow.lexical_search_mock.search_pages.return_value = ()
    uow.lexical_search_mock.quarter_report_titles.return_value = {
        actual.document_id: "Alder Q2 FY2027 earnings release",
        other.document_id: "Beacon Q2 FY2027 earnings release",
    }
    uow.lexical_search_mock.quarter_passages.side_effect = [
        ((QuarterPassage(forecast, frozenset()), QuarterPassage(actual, frozenset())), frozenset()),
        ((QuarterPassage(other, frozenset()),), frozenset()),
    ]
    companies = create_autospec(CompanyPriorityReader, instance=True)
    companies.preferred_documents.side_effect = lambda _context, question, _ids, **_options: {
        "Alder": frozenset({actual.document_id}),
        "Beacon": frozenset({actual.document_id if mode == "ambiguous" else other.document_id}),
    }.get(question, frozenset())
    embedding, dense = make_provider_mocks()
    service = make_service(
        uow,
        embedding_provider=embedding,
        dense_retriever=dense,
        selection_profile=RrfSelectionProfile(context_chunk_limit=2),
        expand_page_context=True,
        company_priority=companies,
        identifiers=(REQUEST_ID, RUN_ID, UUID(int=667), UUID(int=668), UUID(int=669)),
    )
    question = (
        "What did Alder say about Beacon?"
        if mode == "cross_attribution"
        else "What did Alder report as AI server revenue in Q2 FY2027, "
        "and what did Beacon report as total net sales in Q2 FY2027?"
    )
    result = service.retrieve(
        make_context(),
        ResearchRetrievalInput(
            question=question, company_id=UUID(int=670) if mode == "filtered" else None
        ),
    )

    if mode == "comparison":
        assert [item.chunk_id for item in result.sources] == [actual.chunk_id, other.chunk_id]
        assert [
            call.args[-1] for call in uow.lexical_search_mock.quarter_passages.call_args_list
        ] == [frozenset({actual.document_id}), frozenset({other.document_id})]
        assert all(
            item.origin is RetrievalOrigin.STRUCTURED
            for item in result.candidates
            if item.selection_rank
        )
    else:
        assert [item.chunk_id for item in result.sources] == [anchor.chunk_id]
        assert uow.lexical_search_mock.quarter_passages.call_count == (
            1 if mode == "ambiguous" else 0
        )


@pytest.mark.parametrize("compound", [True, False])
def test_rrf_page_context_preserves_observation_date_and_fusion(compound: bool) -> None:
    question = (
        "What operating ARR did IREN report as of September 30, 2026, and when is the target?"
        if compound
        else "What exact operating ARR did IREN report for November 6, 2026?"
    )
    anchor = make_lexical_hit()
    target = anchor.model_copy(update={"chunk_id": UUID(int=601), "page_number": 24})
    measurement = anchor.model_copy(update={"chunk_id": UUID(int=602), "page_number": 22})
    uow = RecordingUnitOfWork(lexical_hits=(anchor,))
    uow.lexical_search_mock.search_pages.return_value = (target,)
    uow.lexical_search_mock.search_observation.return_value = measurement if compound else None
    embedder, dense = make_provider_mocks()
    service = make_service(
        uow,
        embedding_provider=embedder,
        dense_retriever=dense,
        identifiers=(REQUEST_ID, RUN_ID, *(UUID(int=700 + index) for index in range(3))),
        selection_profile=RrfSelectionProfile(context_chunk_limit=16),
        expand_page_context=True,
    )

    result = service.retrieve(make_context(), ResearchRetrievalInput(question=question))

    assert result.retrieval_run.selector_config_version == PAGE_SELECTOR_VERSION
    assert result.retrieval_run.context_chunk_limit == 8
    assert result.candidates[0].fused_rank == 1
    _, _, _, metric, date, selected_pages = (
        uow.lexical_search_mock.search_observation.call_args.args
    )
    assert metric == "operating ARR"
    assert date.isoformat() == ("2026-09-30" if compound else "2026-11-06")
    if compound:
        assert (target.document_version_id, 24) in selected_pages
        assert [item.page_number for item in result.sources] == [2, 24, 22]
        assert [item.rank for item in result.candidates[1:]] == [1, 2]
        assert all(item.origin is RetrievalOrigin.STRUCTURED for item in result.candidates[1:])
    else:
        uow.lexical_search_mock.search_pages.assert_not_called()
        assert [item.chunk_id for item in result.sources] == [anchor.chunk_id]


def test_company_lookup_terms_use_word_boundaries_and_explicit_tickers() -> None:
    names, tickers = company_lookup_terms("Does IREN Limited work with NVIDIA, BRK.B or $AI?")
    assert {"iren", "iren limited", "nvidia", "brk.b"} <= names
    assert {"IREN", "NVIDIA", "BRK.B", "AI"} <= tickers
    assert "IRE" not in tickers
    assert "AI" not in company_lookup_terms("Which AI services are available?")[1]


@pytest.mark.parametrize("manual_filter", [False, True])
def test_company_priority_preserves_cross_company_evidence(manual_filter: bool) -> None:
    first = make_lexical_hit()
    other = first.model_copy(update={"chunk_id": UUID(int=802)})
    preferred = first.model_copy(update={"chunk_id": UUID(int=803), "document_id": UUID(int=804)})
    unit_of_work = RecordingUnitOfWork(lexical_hits=(first, other, preferred))
    unit_of_work.lexical_search_mock.search_pages.return_value = ()
    embedding, dense = make_provider_mocks()
    company_priority = create_autospec(CompanyPriorityReader, instance=True)
    company_priority.preferred_documents.return_value = frozenset({preferred.document_id})
    service = make_service(
        unit_of_work,
        embedding_provider=embedding,
        dense_retriever=dense,
        selection_profile=RrfSelectionProfile(context_chunk_limit=2),
        expand_page_context=True,
        company_priority=company_priority,
        identifiers=(REQUEST_ID, RUN_ID, *(UUID(int=900 + index) for index in range(3))),
    )
    company_filter = UUID(int=805) if manual_filter else None

    result = service.retrieve(
        make_context(),
        ResearchRetrievalInput(
            question="How does NVIDIA work with partners?", company_id=company_filter
        ),
    )

    assert result.retrieval_run.filters.company_id == company_filter
    assert [item.fused_rank for item in result.candidates] == [1, 2, 3]
    if manual_filter:
        company_priority.preferred_documents.assert_not_called()
        assert result.retrieval_run.selector_config_version == PAGE_SELECTOR_VERSION
        assert [item.chunk_id for item in result.sources] == [first.chunk_id, other.chunk_id]
    else:
        company_priority.preferred_documents.assert_called_once_with(
            make_context(),
            "How does NVIDIA work with partners?",
            {first.document_id, preferred.document_id},
        )
        assert result.retrieval_run.selector_config_version == COMPANY_PAGE_RRF_SELECTOR_VERSION
        assert [item.chunk_id for item in result.sources] == [preferred.chunk_id, first.chunk_id]


@pytest.mark.parametrize("ranking", [(2, 3), (2, 2), None])
def test_luna_selects_from_rrf_and_page_hits_with_baseline_fallback(
    ranking: tuple[int, int] | None,
) -> None:
    first = make_lexical_hit()
    second = first.model_copy(update={"chunk_id": UUID(int=850)})
    page = first.model_copy(update={"chunk_id": UUID(int=851), "page_number": 9})
    uow = RecordingUnitOfWork(lexical_hits=(first, second))
    uow.lexical_search_mock.search_pages.return_value = (page,)
    embedding, dense = make_provider_mocks()
    provider = Mock(
        return_value=ListwiseRanking(
            ranking or (),
            ProviderUsage(
                input_units=10,
                output_units=3,
            ),
        )
    )
    executor = make_provider_operation_executor()
    reranker = AccountedLunaReranker(
        provider, executor, "gpt-5.6-luna", ProviderCostPolicy(200_000, 1_200_000)
    )
    service = make_service(
        uow,
        embedding_provider=embedding,
        dense_retriever=dense,
        selection_profile=RrfSelectionProfile(context_chunk_limit=2),
        expand_page_context=True,
        luna_reranker=reranker,
        identifiers=(REQUEST_ID, RUN_ID, *(UUID(int=900 + index) for index in range(3))),
    )

    result = service.retrieve(
        make_context(),
        ResearchRetrievalInput(
            question="What is the result?", rerank_with_luna=ranking is not None
        ),
    )

    if ranking == (2, 3):
        assert result.retrieval_run.selector_config_version == LUNA_RERANK_SELECTOR_VERSION
        assert [source.chunk_id for source in result.sources] == [second.chunk_id, page.chunk_id]
    else:
        assert result.retrieval_run.selector_config_version == PAGE_SELECTOR_VERSION
        assert [source.chunk_id for source in result.sources] == [first.chunk_id, page.chunk_id]
    if ranking is None:
        provider.assert_not_called()
    else:
        provider.assert_called_once()
        assert provider.call_args.args[1:] == (3, 2)
