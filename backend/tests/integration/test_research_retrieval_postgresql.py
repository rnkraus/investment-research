"""PostgreSQL integration tests for atomic research retrieval orchestration."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.infrastructure.database.unit_of_work import (
    SqlAlchemyResearchRetrievalRepositories,
    create_provider_operation_unit_of_work_factory,
    create_research_retrieval_unit_of_work_factory,
)
from investment_research.infrastructure.operations.opentelemetry import create_operation_tracer
from investment_research.infrastructure.search.postgresql import (
    PostgresDenseCandidateQualifier,
    PostgresLexicalSearch,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.application import (
    ExecuteProviderOperationService,
    FinalizeProviderOperationService,
    ProviderBudgetPolicy,
    ProviderCostPolicy,
    ReserveProviderOperationService,
)
from investment_research.modules.operations.domain import (
    ProviderOperationState,
    ProviderUsage,
)
from investment_research.modules.operations.persistence import (
    SqlAlchemyProviderOperationRepository,
)
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.research.application import (
    M1QueryRetrievalDependencies,
    QueryEmbeddingAccountingProfile,
    RetrieveResearchService,
)
from investment_research.modules.research.contracts import ResearchRetrievalInput
from investment_research.modules.research.persistence import SqlAlchemyResearchRepository
from investment_research.modules.research.ports import (
    ResearchRetrievalUnitOfWork,
    ResearchRetrievalUnitOfWorkFactory,
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
    M1SelectionProfile,
)
from investment_research.modules.search.domain import (
    RetrievalCandidate,
    RetrievalMode,
    RetrievalOrigin,
    RetrievalRun,
)
from investment_research.modules.search.persistence import SqlAlchemyRetrievalRepository
from tests.integration.factories import (
    FIXTURE_TIME,
    make_research_source,
    make_searchable_research_source,
    persist_research_source,
)

LEXICAL_CONFIG_VERSION = "m1-english-lexical-v1"
EMBEDDING_CONFIG_VERSION = "openai-text-embedding-3-small-1536-v1"
DENSE_INDEX_CONFIG_VERSION = "pinecone-serverless-cosine-1536-v1"
VECTOR_VALUES = (0.125,) * M1_EMBEDDING_DIMENSIONS


class FixedQueryEmbeddingProvider:
    """Return one deterministic query vector without calling a live provider."""

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=(
                EmbeddingVector(
                    input_id=request.inputs[0].input_id,
                    values=VECTOR_VALUES,
                    embedding_config_version=request.embedding_config_version,
                ),
            ),
            usage=ProviderUsage(input_units=0, output_units=0),
        )


class FixedDenseRetriever:
    """Return controlled provider hits while retaining the real qualification path."""

    def __init__(self, hits: tuple[DenseRetrievalHit, ...]) -> None:
        self._hits = hits

    def retrieve(
        self,
        context: WorkspaceContext,
        query: DenseRetrievalQuery,
    ) -> tuple[DenseRetrievalHit, ...]:
        del context, query
        return self._hits


class FailingRetrievalRepository:
    """Simulate a persistence failure after a real lexical query has executed."""

    def add_result(
        self,
        context: WorkspaceContext,
        run: RetrievalRun,
        candidates: Sequence[RetrievalCandidate],
    ) -> None:
        raise RuntimeError("simulated retrieval persistence failure")

    def get_run_by_id(
        self,
        context: WorkspaceContext,
        retrieval_run_id: UUID,
    ) -> RetrievalRun | None:
        raise NotImplementedError

    def list_candidates(
        self,
        context: WorkspaceContext,
        retrieval_run_id: UUID,
    ) -> Sequence[RetrievalCandidate]:
        raise NotImplementedError


def test_service_commits_request_run_and_ranked_candidates_together(
    integration_database_engine: Engine,
) -> None:
    source = make_research_source(label="Research Retrieval Service")
    persist_research_source(integration_database_engine, source)
    request_id = uuid4()
    retrieval_run_id = uuid4()
    candidate_id = uuid4()
    identifiers = iter((request_id, retrieval_run_id, candidate_id))
    clock = iter(
        (
            FIXTURE_TIME + timedelta(seconds=10),
            FIXTURE_TIME + timedelta(seconds=11),
        )
    )
    session_factory = create_session_factory(integration_database_engine)
    service = RetrieveResearchService(
        unit_of_work_factory=create_research_retrieval_unit_of_work_factory(session_factory),
        search_profile=LexicalSearchProfile(
            lexical_config_version=source.processing_run.lexical_config_version,
            candidate_limit=10,
        ),
        clock=clock.__next__,
        identifier_factory=identifiers.__next__,
    )

    result = service.retrieve(
        source.context,
        ResearchRetrievalInput(
            question="  What was FY2025 REVENUE?  ",
            document_ids=(source.document.id,),
        ),
    )

    assert result.request.id == request_id
    assert result.retrieval_run.id == retrieval_run_id
    assert [candidate.id for candidate in result.candidates] == [candidate_id]
    assert [candidate.chunk_id for candidate in result.candidates] == [source.chunks[0].id]

    session_factory = create_session_factory(integration_database_engine)
    with session_factory() as session:
        research_repository = SqlAlchemyResearchRepository(session)
        retrieval_repository = SqlAlchemyRetrievalRepository(session)
        assert research_repository.get_request_by_id(source.context, request_id) == result.request
        assert (
            retrieval_repository.get_run_by_id(source.context, retrieval_run_id)
            == result.retrieval_run
        )
        assert (
            tuple(retrieval_repository.list_candidates(source.context, retrieval_run_id))
            == result.candidates
        )


def test_dual_lane_service_qualifies_selects_and_commits_both_lanes(
    integration_database_engine: Engine,
) -> None:
    source = make_searchable_research_source(
        label="Dual Lane Research Retrieval",
        primary_text="FY2025 revenue was $184.2 million.",
        lexical_config_version=LEXICAL_CONFIG_VERSION,
    )
    source = replace(
        source,
        processing_run=source.processing_run.model_copy(
            update={
                "state": ProcessingState.SEARCHABLE,
                "embedding_config_version": EMBEDDING_CONFIG_VERSION,
                "dense_index_config_version": DENSE_INDEX_CONFIG_VERSION,
            }
        ),
    )
    persist_research_source(integration_database_engine, source)
    unknown_chunk_id = uuid4()
    request_id = uuid4()
    retrieval_run_id = uuid4()
    candidate_ids = (uuid4(), uuid4(), uuid4())
    identifiers = iter((request_id, retrieval_run_id, *candidate_ids))
    clock = iter(
        (
            FIXTURE_TIME + timedelta(seconds=10),
            FIXTURE_TIME + timedelta(seconds=11),
        )
    )
    session_factory = create_session_factory(integration_database_engine)
    provider_operation_unit_of_work_factory = create_provider_operation_unit_of_work_factory(
        session_factory
    )
    operation_tracer = create_operation_tracer(
        service_name="investment-research-integration-test",
        service_version="0.0.0",
    )
    operation_executor = ExecuteProviderOperationService(
        reservation_service=ReserveProviderOperationService(
            unit_of_work_factory=provider_operation_unit_of_work_factory,
            budget_policy=ProviderBudgetPolicy(monthly_limit_micro_usd=5_000_000),
        ),
        finalization_service=FinalizeProviderOperationService(
            unit_of_work_factory=provider_operation_unit_of_work_factory,
        ),
        tracer=operation_tracer,
        clock=lambda: FIXTURE_TIME + timedelta(seconds=10),
        monotonic_clock=iter((10.0, 10.125)).__next__,
    )
    service = RetrieveResearchService(
        unit_of_work_factory=create_research_retrieval_unit_of_work_factory(session_factory),
        search_profile=LexicalSearchProfile(
            lexical_config_version=LEXICAL_CONFIG_VERSION,
            candidate_limit=10,
        ),
        m1_query_retrieval=M1QueryRetrievalDependencies(
            embedding_provider=FixedQueryEmbeddingProvider(),
            dense_retriever=FixedDenseRetriever(
                (
                    DenseRetrievalHit(chunk_id=source.chunks[0].id, raw_score=0.95),
                    DenseRetrievalHit(chunk_id=source.chunks[1].id, raw_score=0.75),
                    DenseRetrievalHit(chunk_id=unknown_chunk_id, raw_score=0.5),
                )
            ),
            dense_profile=DenseRetrievalProfile(
                embedding_config_version=EMBEDDING_CONFIG_VERSION,
                dense_index_config_version=DENSE_INDEX_CONFIG_VERSION,
                candidate_limit=10,
            ),
            selection_profile=M1SelectionProfile(),
            provider_operation_executor=operation_executor,
            embedding_accounting=QueryEmbeddingAccountingProfile(
                provider="openai",
                model="text-embedding-3-small",
                cost_policy=ProviderCostPolicy(
                    input_micro_usd_per_million_units=20_000,
                    output_micro_usd_per_million_units=0,
                ),
            ),
            retry_delay_seconds=0,
        ),
        clock=clock.__next__,
        identifier_factory=identifiers.__next__,
    )

    try:
        result = service.retrieve(
            source.context,
            ResearchRetrievalInput(
                question="What was FY2025 revenue?",
                document_ids=(source.document.id,),
            ),
        )
    finally:
        operation_tracer.close()

    assert result.retrieval_run.mode is RetrievalMode.DUAL_LANE
    assert [(candidate.origin, candidate.chunk_id) for candidate in result.candidates] == [
        (RetrievalOrigin.LEXICAL, source.chunks[0].id),
        (RetrievalOrigin.DENSE, source.chunks[0].id),
        (RetrievalOrigin.DENSE, source.chunks[1].id),
    ]
    assert [candidate.selection_rank for candidate in result.candidates] == [1, None, 2]
    assert [selected.chunk_id for selected in result.sources] == [
        source.chunks[0].id,
        source.chunks[1].id,
    ]
    assert unknown_chunk_id not in {candidate.chunk_id for candidate in result.candidates}

    with session_factory() as session:
        research_repository = SqlAlchemyResearchRepository(session)
        retrieval_repository = SqlAlchemyRetrievalRepository(session)
        operations = SqlAlchemyProviderOperationRepository(session).list_by_correlation_id(
            source.context,
            request_id,
        )
        assert research_repository.get_request_by_id(source.context, request_id) == result.request
        assert (
            retrieval_repository.get_run_by_id(source.context, retrieval_run_id)
            == result.retrieval_run
        )
        assert tuple(retrieval_repository.list_candidates(source.context, retrieval_run_id)) == (
            result.candidates[0],
            result.candidates[2],
            result.candidates[1],
        )
        assert len(operations) == 1
        assert operations[0].state is ProviderOperationState.SUCCEEDED
        assert operations[0].research_request_id == request_id
        assert operations[0].input_units == 0
        assert operations[0].actual_cost_micro_usd == 0
        assert len(operations[0].trace_id) == 32


def test_failure_after_real_search_rolls_back_the_flushed_request(
    integration_database_engine: Engine,
) -> None:
    source = make_research_source(label="Research Retrieval Rollback")
    persist_research_source(integration_database_engine, source)
    request_id = uuid4()
    retrieval_run_id = uuid4()
    candidate_id = uuid4()
    identifiers = iter((request_id, retrieval_run_id, candidate_id))
    clock = iter(
        (
            FIXTURE_TIME + timedelta(seconds=10),
            FIXTURE_TIME + timedelta(seconds=11),
        )
    )
    session_factory = create_session_factory(integration_database_engine)

    @contextmanager
    def failing_unit_of_work() -> Iterator[ResearchRetrievalUnitOfWork]:
        with transactional_session(session_factory) as session:
            yield SqlAlchemyResearchRetrievalRepositories(
                research=SqlAlchemyResearchRepository(session),
                lexical_search=PostgresLexicalSearch(session),
                dense_qualifier=PostgresDenseCandidateQualifier(session),
                retrieval=FailingRetrievalRepository(),
            )

    unit_of_work_factory: ResearchRetrievalUnitOfWorkFactory = failing_unit_of_work
    service = RetrieveResearchService(
        unit_of_work_factory=unit_of_work_factory,
        search_profile=LexicalSearchProfile(
            lexical_config_version=source.processing_run.lexical_config_version,
            candidate_limit=10,
        ),
        clock=clock.__next__,
        identifier_factory=identifiers.__next__,
    )

    with pytest.raises(RuntimeError, match="simulated retrieval persistence failure"):
        service.retrieve(
            source.context,
            ResearchRetrievalInput(
                question="What was FY2025 revenue?",
                document_ids=(source.document.id,),
            ),
        )

    with session_factory() as session:
        assert (
            SqlAlchemyResearchRepository(session).get_request_by_id(
                source.context,
                request_id,
            )
            is None
        )
        assert (
            SqlAlchemyRetrievalRepository(session).get_run_by_id(
                source.context,
                retrieval_run_id,
            )
            is None
        )
