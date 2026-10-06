"""Unit tests for M1 dual-lane research retrieval orchestration."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast
from unittest.mock import Mock, create_autospec
from uuid import UUID

import pytest

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.application import (
    ExecuteProviderOperationService,
    ProviderBudgetExceededError,
    ProviderCostPolicy,
    ProviderUsageCostExceededReservationError,
)
from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.research.application import (
    M1QueryRetrievalDependencies,
    QueryEmbeddingAccountingProfile,
    ResearchProviderUnavailableError,
    RetrieveResearchService,
)
from investment_research.modules.research.contracts import ResearchRetrievalInput
from investment_research.modules.research.ports import (
    ResearchRepository,
    ResearchRetrievalUnitOfWork,
    ResearchRetrievalUnitOfWorkFactory,
)
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
)
from investment_research.modules.search.domain import RetrievalMode, RetrievalOrigin
from investment_research.modules.search.fusion_contracts import (
    RRF_CONFIG_VERSION,
    RrfSelectionProfile,
)
from investment_research.modules.search.ports import (
    DenseCandidateQualifier,
    DenseRetriever,
    EmbeddingProvider,
    LexicalSearch,
    RetrievalRepository,
    SearchProviderConfigurationError,
    SearchProviderTransientError,
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
        self.dense_qualifier_mock.qualify.return_value = dense_hits
        self.events = events if events is not None else []
        self.enter_count = 0

    @contextmanager
    def open(self) -> Iterator[ResearchRetrievalUnitOfWork]:
        """Record when the short database transaction begins."""

        self.enter_count += 1
        self.events.append("database")
        yield self


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
    selection_profile: M1SelectionProfile | RrfSelectionProfile | None = None,
    lexical_limit: int = 10,
    dense_limit: int = 10,
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
        ),
        clock=clock_values.__next__,
        identifier_factory=identifier_values.__next__,
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
    service = make_service(
        unit_of_work,
        embedding_provider=cast(EmbeddingProvider, embedding),
        dense_retriever=cast(DenseRetriever, dense_provider),
        selection_profile=RrfSelectionProfile(context_chunk_limit=context_chunk_limit),
        identifiers=(
            REQUEST_ID,
            RUN_ID,
            LEXICAL_CANDIDATE_ID,
            DENSE_SELECTED_CANDIDATE_ID,
            DENSE_DUPLICATE_CANDIDATE_ID,
        ),
    )

    result = service.retrieve(make_context(), ResearchRetrievalInput(question="revenue margin"))

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


def test_usage_above_reservation_is_not_retriable_and_stops_before_dense_search() -> None:
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
        raise ProviderUsageCostExceededReservationError(
            "provider usage cost exceeded its reservation"
        )

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
    assert isinstance(
        captured.value.__cause__,
        ProviderUsageCostExceededReservationError,
    )
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
