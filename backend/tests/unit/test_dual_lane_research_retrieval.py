"""Unit tests for M1 dual-lane research retrieval orchestration."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast
from unittest.mock import Mock, create_autospec
from uuid import UUID

import pytest

from investment_research.modules.documents.domain import SourceLocatorKind
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.research.application import (
    M1QueryRetrievalDependencies,
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


def make_service(
    unit_of_work: RecordingUnitOfWork,
    *,
    embedding_provider: EmbeddingProvider,
    dense_retriever: DenseRetriever,
    identifiers: tuple[UUID, ...] = (REQUEST_ID, RUN_ID),
    sleeper: Mock | None = None,
) -> RetrieveResearchService:
    """Build one deterministic dual-lane service."""

    identifier_values = iter(identifiers)
    clock_values = iter((STARTED_AT, COMPLETED_AT))
    unit_of_work_factory: ResearchRetrievalUnitOfWorkFactory = unit_of_work.open
    return RetrieveResearchService(
        unit_of_work_factory=unit_of_work_factory,
        search_profile=LexicalSearchProfile(
            lexical_config_version="m1-english-lexical-v1",
            candidate_limit=10,
        ),
        m1_query_retrieval=M1QueryRetrievalDependencies(
            embedding_provider=embedding_provider,
            dense_retriever=dense_retriever,
            dense_profile=DenseRetrievalProfile(
                embedding_config_version=EMBEDDING_CONFIG_VERSION,
                dense_index_config_version=DENSE_INDEX_CONFIG_VERSION,
                candidate_limit=10,
            ),
            selection_profile=M1SelectionProfile(),
            retry_delay_seconds=0,
            sleeper=sleeper or Mock(),
        ),
        clock=clock_values.__next__,
        identifier_factory=identifier_values.__next__,
    )


def test_dual_lane_retrieval_runs_providers_before_one_atomic_database_phase() -> None:
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
    )

    result = service.retrieve(
        make_context(),
        ResearchRetrievalInput(
            question="How did sales and profitability develop?",
            document_ids=(DOCUMENT_ID,),
        ),
    )

    assert events == ["embedding", "pinecone", "database"]
    assert unit_of_work.enter_count == 1
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

    embedding_request = embedding_mock.embed.call_args.args[0]
    assert embedding_request.inputs[0].input_id == REQUEST_ID
    assert embedding_request.inputs[0].purpose.value == "QUERY"
    assert embedding_request.inputs[0].text == "How did sales and profitability develop?"
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
    assert unit_of_work.enter_count == 1


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

    with pytest.raises(type(provider_error)):
        service.retrieve(
            make_context(),
            ResearchRetrievalInput(question="What was FY2025 revenue?"),
        )

    assert unit_of_work.enter_count == 0
    unit_of_work.research_mock.add_request.assert_not_called()
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
            retry_delay_seconds=0.5,
        )
