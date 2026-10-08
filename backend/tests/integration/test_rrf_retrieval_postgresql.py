"""Verify opt-in RRF/reranking through canonical reads, accounting, and persistence."""

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select

from investment_research.infrastructure.database.session import create_session_factory
from investment_research.infrastructure.database.unit_of_work import (
    create_provider_operation_unit_of_work_factory,
    create_research_retrieval_unit_of_work_factory,
)
from investment_research.infrastructure.operations.noop import NoopOperationTracer
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.application import (
    ExecuteProviderOperationService,
    FinalizeProviderOperationService,
    ProviderBudgetPolicy,
    ProviderCostPolicy,
    RerankingRequestQuotaPolicy,
    ReserveProviderOperationService,
)
from investment_research.modules.operations.domain import (
    ProviderOperationErrorCode,
    ProviderOperationKind,
    ProviderOperationState,
    ProviderUsage,
)
from investment_research.modules.operations.persistence import ProviderOperationRow
from investment_research.modules.processing.application import M1_PROCESSING_PROFILE
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.processing.persistence import ChunkSetRow
from investment_research.modules.research.contracts import ResearchRetrievalInput
from investment_research.modules.research.domain import TemporalMode
from investment_research.modules.research.errors import ResearchProviderUnavailableError
from investment_research.modules.research.reranking import (
    AccountedRerankingService,
    RerankingAccountingProfile,
)
from investment_research.modules.research.retrieval import (
    M1QueryRetrievalDependencies,
    QueryEmbeddingAccountingProfile,
    RetrieveResearchService,
)
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    DenseRetrievalHit,
    DenseRetrievalProfile,
    DenseRetrievalQuery,
    EmbeddingRequest,
    EmbeddingResult,
    EmbeddingVector,
    LexicalSearchProfile,
    RerankRequest,
    RerankResponse,
    RerankResult,
)
from investment_research.modules.search.domain import RetrievalOrigin
from investment_research.modules.search.fusion_contracts import (
    RRF_CONFIG_VERSION,
    RrfSelectionProfile,
)
from investment_research.modules.search.persistence import (
    RetrievalRunRow,
    SqlAlchemyRetrievalRepository,
)
from investment_research.modules.search.ports import Reranker, SearchProviderTransientError
from investment_research.modules.search.reranking_contracts import (
    BGE_RERANK_CONFIG_VERSION,
    BGE_RERANK_MODEL,
    RrfRerankingProfile,
)
from tests.integration.factories import (
    FIXTURE_TIME,
    ResearchSource,
    make_searchable_research_source,
    persist_research_source,
)


class FixedEmbeddingProvider:
    """Return a dimension-valid test vector without network access or spend."""

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=(
                EmbeddingVector(
                    input_id=request.inputs[0].input_id,
                    values=(0.125,) * M1_EMBEDDING_DIMENSIONS,
                    embedding_config_version=request.embedding_config_version,
                ),
            ),
            usage=ProviderUsage(input_units=0, output_units=0),
        )


class FixedDenseRetriever:
    """Supply controlled untrusted hits; PostgreSQL must qualify each one."""

    def __init__(self, hits: tuple[DenseRetrievalHit, ...]) -> None:
        self.hits = hits

    def retrieve(
        self, context: WorkspaceContext, query: DenseRetrievalQuery
    ) -> tuple[DenseRetrievalHit, ...]:
        del context, query
        return self.hits


def make_source(label: str) -> ResearchSource:
    """Build a canonical source eligible for both existing M1 lanes."""

    source = make_searchable_research_source(
        label=label,
        primary_text="FY2025 revenue was $184.2 million.",
        lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
    )
    return replace(
        source,
        processing_run=source.processing_run.model_copy(
            update={
                "state": ProcessingState.SEARCHABLE,
                "embedding_config_version": M1_PROCESSING_PROFILE.embedding_config_version,
                "dense_index_config_version": M1_PROCESSING_PROFILE.dense_index_config_version,
            }
        ),
    )


def make_service(
    engine: Engine,
    hits: tuple[DenseRetrievalHit, ...],
    *,
    reranker: Reranker | None = None,
    quota_limit: int | None = 1,
    monthly_budget: int = 5_000_000,
) -> RetrieveResearchService:
    """Inject RRF explicitly while retaining real transactions and provider accounting."""

    session_factory = create_session_factory(engine)
    operation_factory = create_provider_operation_unit_of_work_factory(session_factory)
    executor = ExecuteProviderOperationService(
        reservation_service=ReserveProviderOperationService(
            unit_of_work_factory=operation_factory,
            budget_policy=ProviderBudgetPolicy(monthly_limit_micro_usd=monthly_budget),
            reranking_quota_policy=(
                RerankingRequestQuotaPolicy(quota_limit)
                if reranker is not None and quota_limit is not None
                else None
            ),
        ),
        finalization_service=FinalizeProviderOperationService(
            unit_of_work_factory=operation_factory
        ),
        tracer=NoopOperationTracer(),
    )
    return RetrieveResearchService(
        unit_of_work_factory=create_research_retrieval_unit_of_work_factory(session_factory),
        search_profile=LexicalSearchProfile(
            lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
            candidate_limit=20,
        ),
        m1_query_retrieval=M1QueryRetrievalDependencies(
            embedding_provider=FixedEmbeddingProvider(),
            dense_retriever=FixedDenseRetriever(hits),
            dense_profile=DenseRetrievalProfile(
                embedding_config_version=M1_PROCESSING_PROFILE.embedding_config_version,
                dense_index_config_version=M1_PROCESSING_PROFILE.dense_index_config_version,
                candidate_limit=20,
            ),
            selection_profile=RrfSelectionProfile() if reranker is None else RrfRerankingProfile(),
            provider_operation_executor=executor,
            embedding_accounting=QueryEmbeddingAccountingProfile(
                provider="deterministic",
                model="rrf-test",
                cost_policy=ProviderCostPolicy(
                    input_micro_usd_per_million_units=0,
                    output_micro_usd_per_million_units=0,
                ),
            ),
            reranking=(
                AccountedRerankingService(
                    reranker=reranker,
                    executor=executor,
                    accounting=RerankingAccountingProfile(
                        provider="deterministic",
                        # Synthetic one-micro-USD/unit accounting, not approved live pricing.
                        cost_policy=ProviderCostPolicy(1_000_000, 0),
                        maximum_usage=ProviderUsage(input_units=1, output_units=0),
                    ),
                    public_synthetic_content_approved=True,
                )
                if reranker is not None
                else None
            ),
        ),
    )


class RecordingReranker:
    """Return a reverse ranking while exposing an optional deterministic race/failure."""

    def __init__(self, during_call: Callable[[], None] = lambda: None, *, units: int = 1) -> None:
        self.during_call = during_call
        self.units = units
        self.requests: list[RerankRequest] = []

    def rerank(self, request: RerankRequest) -> RerankResponse:
        self.requests.append(request)
        self.during_call()
        return RerankResponse(
            results=tuple(
                RerankResult(
                    retrieval_candidate_id=item.retrieval_candidate_id, rank=rank, score=1 / rank
                )
                for rank, item in enumerate(reversed(request.candidates), 1)
            ),
            model=BGE_RERANK_MODEL,
            reranker_config_version=BGE_RERANK_CONFIG_VERSION,
            usage=ProviderUsage(input_units=self.units, output_units=0),
        )


def test_reranking_commits_reservation_then_persists_complete_canonical_scores(
    integration_database_engine: Engine,
) -> None:
    source = make_source("Accounted reranking eligible")
    foreign = make_source("Accounted reranking foreign")
    persist_research_source(integration_database_engine, source)
    persist_research_source(integration_database_engine, foreign)
    factory = create_session_factory(integration_database_engine)

    def inspect_reservation() -> None:
        with factory() as session:
            operation = session.scalar(
                select(ProviderOperationRow).where(
                    ProviderOperationRow.workspace_id == source.context.workspace_id,
                    ProviderOperationRow.operation == ProviderOperationKind.RERANKING,
                )
            )
            assert operation is not None and operation.state is ProviderOperationState.STARTED

    reranker = RecordingReranker(inspect_reservation)
    service = make_service(
        integration_database_engine,
        (
            DenseRetrievalHit(chunk_id=foreign.chunks[0].id, raw_score=0.99),
            DenseRetrievalHit(chunk_id=uuid4(), raw_score=0.98),
            DenseRetrievalHit(chunk_id=source.chunks[1].id, raw_score=0.9),
            DenseRetrievalHit(chunk_id=source.chunks[0].id, raw_score=0.8),
        ),
        reranker=reranker,
    )
    result = service.retrieve(source.context, ResearchRetrievalInput(question="revenue"))
    assert len(reranker.requests) == 1
    assert {item.retrieval_candidate_id for item in reranker.requests[0].candidates} == {
        chunk.id for chunk in source.chunks
    }
    assert [item.chunk_id for item in result.sources] == [source.chunks[1].id, source.chunks[0].id]
    assert all(item.reranker_score is not None for item in result.candidates)
    with factory() as session:
        repo = SqlAlchemyRetrievalRepository(session)
        assert {
            item.id: item for item in repo.list_candidates(source.context, result.retrieval_run.id)
        } == {item.id: item for item in result.candidates}
        assert repo.list_candidates(foreign.context, result.retrieval_run.id) == ()
        operation = session.scalar(
            select(ProviderOperationRow).where(
                ProviderOperationRow.research_request_id == result.request.id,
                ProviderOperationRow.operation == ProviderOperationKind.RERANKING,
            )
        )
        assert operation is not None and operation.state is ProviderOperationState.SUCCEEDED
        assert operation.actual_cost_micro_usd == operation.reserved_cost_micro_usd == 1
        assert operation.input_units == 1 and operation.output_units == 0


def test_source_change_during_reranking_aborts_persistence_without_refunding_usage(
    integration_database_engine: Engine,
) -> None:
    source = make_source("Reranking state race")
    persist_research_source(integration_database_engine, source)
    factory = create_session_factory(integration_database_engine)

    def deactivate() -> None:
        with factory.begin() as session:
            row = session.get(ChunkSetRow, source.chunk_set.id)
            assert row is not None
            row.is_active = False

    reranker = RecordingReranker(deactivate)
    service = make_service(integration_database_engine, (), reranker=reranker)
    with pytest.raises(ResearchProviderUnavailableError) as captured:
        service.retrieve(source.context, ResearchRetrievalInput(question="revenue"))
    assert not captured.value.retriable and len(reranker.requests) == 1
    with factory() as session:
        operation = session.scalar(
            select(ProviderOperationRow).where(
                ProviderOperationRow.research_request_id == captured.value.research_request_id,
                ProviderOperationRow.operation == ProviderOperationKind.RERANKING,
            )
        )
        assert operation is not None and operation.state is ProviderOperationState.SUCCEEDED
        assert operation.actual_cost_micro_usd == 1
        assert (
            session.scalar(
                select(RetrievalRunRow).where(
                    RetrievalRunRow.research_request_id == captured.value.research_request_id
                )
            )
            is None
        )


@pytest.mark.parametrize("kind", ["quota", "missing-quota", "budget"])
def test_durable_reranking_limits_prevent_the_call(
    integration_database_engine: Engine, kind: str
) -> None:
    source = make_source(f"Reranking blocked {kind}")
    persist_research_source(integration_database_engine, source)
    reranker = RecordingReranker()
    service = make_service(
        integration_database_engine,
        (),
        reranker=reranker,
        quota_limit=None if kind == "missing-quota" else 0 if kind == "quota" else 1,
        monthly_budget=0 if kind == "budget" else 5_000_000,
    )
    with pytest.raises(ResearchProviderUnavailableError):
        service.retrieve(source.context, ResearchRetrievalInput(question="revenue"))
    assert reranker.requests == []


@pytest.mark.parametrize("kind", ["transient", "usage"])
def test_reranking_failure_finalizes_one_attempt_without_retry(
    integration_database_engine: Engine, kind: str
) -> None:
    source = make_source(f"Reranking failed {kind}")
    persist_research_source(integration_database_engine, source)

    def fail() -> None:
        if kind == "transient":
            raise SearchProviderTransientError("synthetic unavailable")

    reranker = RecordingReranker(fail, units=2 if kind == "usage" else 1)
    service = make_service(integration_database_engine, (), reranker=reranker)
    with pytest.raises(ResearchProviderUnavailableError) as captured:
        service.retrieve(source.context, ResearchRetrievalInput(question="revenue"))
    assert len(reranker.requests) == 1
    assert captured.value.retriable is (kind == "transient")
    with create_session_factory(integration_database_engine)() as session:
        operation = session.scalar(
            select(ProviderOperationRow).where(
                ProviderOperationRow.research_request_id == captured.value.research_request_id,
                ProviderOperationRow.operation == ProviderOperationKind.RERANKING,
            )
        )
        assert operation is not None and operation.state is ProviderOperationState.FAILED
        assert operation.actual_cost_micro_usd == operation.reserved_cost_micro_usd == 1
        assert operation.safe_error_code is (
            ProviderOperationErrorCode.UNAVAILABLE
            if kind == "transient"
            else ProviderOperationErrorCode.CONTRACT
        )


def test_no_eligible_sources_consumes_no_reranking_quota(
    integration_database_engine: Engine,
) -> None:
    source = make_source("Reranking empty eligibility")
    persist_research_source(integration_database_engine, source)
    reranker = RecordingReranker()
    service = make_service(integration_database_engine, (), reranker=reranker, quota_limit=0)
    result = service.retrieve(
        source.context, ResearchRetrievalInput(question="unmatchedsyntheticterm")
    )
    assert result.sources == ()
    assert result.candidates == ()
    assert reranker.requests == []
    with create_session_factory(integration_database_engine)() as session:
        assert (
            session.scalar(
                select(ProviderOperationRow.id).where(
                    ProviderOperationRow.research_request_id == result.request.id,
                    ProviderOperationRow.operation == ProviderOperationKind.RERANKING,
                )
            )
            is None
        )


def test_rrf_round_trips_all_lane_ranks_and_canonical_source_ids(
    integration_database_engine: Engine,
) -> None:
    source = make_source("RRF eligible")
    foreign = make_source("RRF foreign")
    persist_research_source(integration_database_engine, source)
    persist_research_source(integration_database_engine, foreign)
    service = make_service(
        integration_database_engine,
        (
            DenseRetrievalHit(chunk_id=foreign.chunks[0].id, raw_score=0.99),
            DenseRetrievalHit(chunk_id=uuid4(), raw_score=0.98),
            DenseRetrievalHit(chunk_id=source.chunks[1].id, raw_score=-0.1),
            DenseRetrievalHit(chunk_id=source.chunks[0].id, raw_score=-0.2),
        ),
    )

    result = service.retrieve(source.context, ResearchRetrievalInput(question="revenue"))

    assert result.retrieval_run.selector_config_version == RRF_CONFIG_VERSION
    assert len(result.candidates) == 3
    assert [item.chunk_id for item in result.sources] == [chunk.id for chunk in source.chunks]
    first = [item for item in result.candidates if item.chunk_id == source.chunks[0].id]
    assert [item.rank for item in first] == [1, 2]
    assert [item.fused_rank for item in first] == [1, 1]
    assert [item.selection_rank for item in first] == [1, None]
    assert first[0].origin is RetrievalOrigin.LEXICAL
    assert result.sources[0].retrieval_candidate_id == first[0].id
    assert result.sources[0].text == source.chunks[0].text
    session_factory = create_session_factory(integration_database_engine)
    with session_factory() as session:
        repository = SqlAlchemyRetrievalRepository(session)
        assert (
            repository.get_run_by_id(source.context, result.retrieval_run.id)
            == result.retrieval_run
        )
        stored = repository.list_candidates(source.context, result.retrieval_run.id)
        assert {item.id: item for item in stored} == {item.id: item for item in result.candidates}
        assert repository.get_run_by_id(foreign.context, result.retrieval_run.id) is None
        assert repository.list_candidates(foreign.context, result.retrieval_run.id) == ()


@pytest.mark.parametrize("excluded", ["inactive", "temporal", "document-filter"])
def test_rrf_ineligible_hits_never_contribute_to_persisted_fusion(
    integration_database_engine: Engine,
    excluded: str,
) -> None:
    source = make_source(f"RRF excluded {excluded}")
    persist_research_source(integration_database_engine, source)
    request = ResearchRetrievalInput(question="revenue")
    if excluded == "inactive":
        session_factory = create_session_factory(integration_database_engine)
        with session_factory.begin() as session:
            row = session.scalar(select(ChunkSetRow).where(ChunkSetRow.id == source.chunk_set.id))
            assert row is not None
            row.is_active = False
    elif excluded == "temporal":
        request = ResearchRetrievalInput(
            question="revenue",
            temporal_mode=TemporalMode.PUBLIC_KNOWLEDGE,
            cutoff_at=FIXTURE_TIME - timedelta(seconds=1),
        )
    else:
        request = ResearchRetrievalInput(question="revenue", document_ids=(uuid4(),))
    service = make_service(
        integration_database_engine,
        (DenseRetrievalHit(chunk_id=source.chunks[0].id, raw_score=0.99),),
    )

    result = service.retrieve(source.context, request)

    assert result.candidates == ()
    assert result.sources == ()
    with create_session_factory(integration_database_engine)() as session:
        repository = SqlAlchemyRetrievalRepository(session)
        assert (
            repository.get_run_by_id(source.context, result.retrieval_run.id)
            == result.retrieval_run
        )
        assert repository.list_candidates(source.context, result.retrieval_run.id) == ()
