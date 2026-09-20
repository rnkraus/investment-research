"""PostgreSQL API acceptance tests for deterministic M1 retrieval and generation."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from investment_research import bootstrap as bootstrap_module
from investment_research.api.dependencies import get_workspace_context
from investment_research.infrastructure.database.session import create_session_factory
from investment_research.infrastructure.evidence.openai import OpenAIStatementSupportEvaluator
from investment_research.infrastructure.generation.deterministic import (
    M0_0_FIXTURE_ANSWER_CASE,
)
from investment_research.infrastructure.generation.openai import OpenAIAnswerGenerator
from investment_research.infrastructure.search.openai import OpenAIEmbeddingProvider
from investment_research.infrastructure.search.pinecone import PineconeDenseRetriever
from investment_research.main import create_app
from investment_research.modules.documents.domain import OriginalMediaType, SourceLocatorKind
from investment_research.modules.evidence.contracts import (
    StatementSupportDecision,
    StatementSupportResult,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.domain import (
    ProviderOperation,
    ProviderOperationKind,
    ProviderOperationState,
    ProviderUsage,
)
from investment_research.modules.operations.persistence import (
    SqlAlchemyProviderOperationRepository,
)
from investment_research.modules.processing.application import M1_PROCESSING_PROFILE
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    AnswerGenerationResult,
    UnvalidatedEvidenceReference,
    UnvalidatedGeneratedAnswer,
    UnvalidatedGeneratedStatement,
)
from investment_research.modules.research.domain import CoverageStatus
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    DenseRetrievalHit,
    DenseRetrievalQuery,
    EmbeddingRequest,
    EmbeddingResult,
    EmbeddingVector,
)
from investment_research.modules.search.domain import RetrievalMode, RetrievalOrigin
from investment_research.modules.search.persistence import (
    RetrievalCandidateRow,
    RetrievalRunRow,
)
from investment_research.settings import (
    ApiSettings,
    DenseProviderSettings,
    GenerationProviderSettings,
)
from tests.integration.factories import (
    ResearchSource,
    make_searchable_research_source,
    persist_research_source,
)

SEMANTIC_QUESTION = "What is mainly holding back Northstar's rollout to new clients?"
SEMANTIC_PASSAGE = (
    "The primary constraint on new customer deployments is a shortage of high-voltage transformers."
)
VECTOR_VALUES = (0.125,) * M1_EMBEDDING_DIMENSIONS
SAFE_MISSING_INFORMATION = "The selected archive sources do not answer this question."


class FixedQueryEmbeddingProvider:
    """Return one deterministic query vector without provider network access."""

    def __init__(self) -> None:
        self.requests: list[EmbeddingRequest] = []
        self.closed = False

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        self.requests.append(request)
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

    def close(self) -> None:
        self.closed = True


class FixedDenseRetriever:
    """Return controlled untrusted vector hits without provider network access."""

    def __init__(self, hits: tuple[DenseRetrievalHit, ...]) -> None:
        self._hits = hits
        self.queries: list[DenseRetrievalQuery] = []
        self.closed = False

    def retrieve(
        self,
        context: WorkspaceContext,
        query: DenseRetrievalQuery,
    ) -> tuple[DenseRetrievalHit, ...]:
        del context
        self.queries.append(query)
        return self._hits

    def close(self) -> None:
        self.closed = True


class FixedAnswerGenerator:
    """Return one controlled typed proposal without contacting a model provider."""

    def __init__(self, statement: str | None) -> None:
        self._statement = statement
        self.requests: list[AnswerGenerationInput] = []
        self.closed = False

    def generate(self, generation_input: AnswerGenerationInput) -> AnswerGenerationResult:
        self.requests.append(generation_input)
        if self._statement is None or not generation_input.sources:
            return AnswerGenerationResult(
                answer=UnvalidatedGeneratedAnswer(
                    coverage_status=CoverageStatus.INSUFFICIENT,
                    rendered_text=SAFE_MISSING_INFORMATION,
                    statements=(),
                    missing_information=SAFE_MISSING_INFORMATION,
                ),
                usage=ProviderUsage(input_units=120, output_units=30),
            )

        source = generation_input.sources[0]
        return AnswerGenerationResult(
            answer=UnvalidatedGeneratedAnswer(
                coverage_status=CoverageStatus.COMPLETE,
                rendered_text="Untrusted provider rendering that must not be published directly.",
                statements=(
                    UnvalidatedGeneratedStatement(
                        text=self._statement,
                        evidence=(
                            UnvalidatedEvidenceReference(
                                retrieval_candidate_id=source.retrieval_candidate_id,
                                start_character=0,
                                end_character=len(source.text),
                                exact_text=source.text,
                            ),
                        ),
                    ),
                ),
            ),
            usage=ProviderUsage(input_units=120, output_units=30),
        )

    def close(self) -> None:
        self.closed = True


class FixedStatementSupportEvaluator:
    """Return one controlled independent support decision without provider access."""

    def __init__(self, *, supported: bool) -> None:
        self._supported = supported
        self.requests: list[tuple[str, tuple[str, ...]]] = []
        self.closed = False

    def evaluate(
        self,
        statement: str,
        evidence_texts: Sequence[str],
    ) -> StatementSupportResult:
        self.requests.append((statement, tuple(evidence_texts)))
        return StatementSupportResult(
            decision=StatementSupportDecision(supported=self._supported),
            usage=ProviderUsage(input_units=90, output_units=10),
        )

    def close(self) -> None:
        self.closed = True


@dataclass(frozen=True, slots=True)
class M1ProviderFakes:
    """The complete network-free M1 API provider group used by one test app."""

    embedding: FixedQueryEmbeddingProvider
    dense: FixedDenseRetriever
    generator: FixedAnswerGenerator
    support: FixedStatementSupportEvaluator


def make_m1_api_settings(settings: ApiSettings, object_storage_root: Path) -> ApiSettings:
    """Enable complete synthetic M1 provider configuration for one test API."""

    return settings.model_copy(
        update={
            "object_storage_root": object_storage_root,
            "provider_monthly_budget_micro_usd": 5_000_000,
            "query_providers": DenseProviderSettings.model_validate(
                {
                    "embedding": {"api_key": "synthetic-query-embedding-key"},
                    "pinecone": {
                        "api_key": "synthetic-query-pinecone-key",
                        "index_host": "synthetic-index.example.svc.pinecone.io",
                    },
                }
            ),
            "generation_provider": GenerationProviderSettings.model_validate(
                {"api_key": "synthetic-generation-key"}
            ),
        }
    )


def make_searchable_source(
    *,
    label: str,
    primary_text: str,
    pasted_text: bool = False,
) -> ResearchSource:
    """Build one source eligible for both M1 retrieval lanes."""

    source = make_searchable_research_source(
        label=label,
        primary_text=primary_text,
        lexical_config_version=M1_PROCESSING_PROFILE.lexical_config_version,
    )
    blocks = source.blocks
    original = source.original
    if pasted_text:
        offsets = (0, len(primary_text) + 1)
        blocks = tuple(
            block.model_copy(
                update={
                    "source_locator_kind": SourceLocatorKind.TEXT_PASSAGE,
                    "page_number": None,
                    "start_character": offsets[index],
                    "end_character": offsets[index] + len(block.text),
                }
            )
            for index, block in enumerate(source.blocks)
        )
        original = source.original.model_copy(
            update={
                "storage_key": f"workspaces/{source.context.workspace_id}/originals/source.txt",
                "media_type": OriginalMediaType.PLAIN_TEXT,
            }
        )
    return replace(
        source,
        original=original,
        processing_run=source.processing_run.model_copy(
            update={
                "state": ProcessingState.SEARCHABLE,
                "embedding_config_version": M1_PROCESSING_PROFILE.embedding_config_version,
                "dense_index_config_version": M1_PROCESSING_PROFILE.dense_index_config_version,
            }
        ),
        blocks=blocks,
    )


def install_provider_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hits: tuple[DenseRetrievalHit, ...],
    statement: str | None,
    supported: bool = True,
) -> M1ProviderFakes:
    """Install process-owned deterministic provider doubles at the composition root."""

    embedding_provider = FixedQueryEmbeddingProvider()
    dense_retriever = FixedDenseRetriever(hits)
    answer_generator = FixedAnswerGenerator(statement)
    support_evaluator = FixedStatementSupportEvaluator(supported=supported)
    monkeypatch.setattr(
        bootstrap_module,
        "create_openai_embedding_provider",
        lambda **_: cast(OpenAIEmbeddingProvider, embedding_provider),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "create_pinecone_dense_retriever",
        lambda **_: cast(PineconeDenseRetriever, dense_retriever),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "create_openai_answer_generator",
        lambda **_: cast(OpenAIAnswerGenerator, answer_generator),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "create_openai_statement_support_evaluator",
        lambda **_: cast(OpenAIStatementSupportEvaluator, support_evaluator),
    )
    return M1ProviderFakes(
        embedding=embedding_provider,
        dense=dense_retriever,
        generator=answer_generator,
        support=support_evaluator,
    )


def assert_provider_fakes_closed(fakes: M1ProviderFakes) -> None:
    """Require the API lifespan to release every process-owned provider client."""

    assert fakes.embedding.closed
    assert fakes.dense.closed
    assert fakes.generator.closed
    assert fakes.support.closed


def parse_server_sent_events(body: str) -> list[dict[str, Any]]:
    """Decode completed server-sent events into test-visible dictionaries."""

    return [
        {
            "event_name": lines[0].removeprefix("event: "),
            "payload": json.loads(lines[1].removeprefix("data: ")),
        }
        for frame in body.strip().split("\n\n")
        if (lines := frame.splitlines())
    ]


def load_retrieval_result(
    engine: Engine,
    *,
    workspace_id: UUID,
    research_request_id: UUID,
) -> tuple[RetrievalRunRow, tuple[RetrievalCandidateRow, ...]]:
    """Load one API request's persisted run and candidates in final selection order."""

    session_factory = create_session_factory(engine)
    with session_factory() as session:
        run = session.scalar(
            select(RetrievalRunRow).where(
                RetrievalRunRow.workspace_id == workspace_id,
                RetrievalRunRow.research_request_id == research_request_id,
            )
        )
        assert run is not None
        candidates = tuple(
            session.scalars(
                select(RetrievalCandidateRow)
                .where(
                    RetrievalCandidateRow.workspace_id == workspace_id,
                    RetrievalCandidateRow.retrieval_run_id == run.id,
                )
                .order_by(
                    RetrievalCandidateRow.selection_rank.asc().nulls_last(),
                    RetrievalCandidateRow.origin,
                    RetrievalCandidateRow.rank,
                )
            )
        )
        return run, candidates


def load_provider_operations(
    engine: Engine,
    *,
    context: WorkspaceContext,
    research_request_id: UUID,
) -> tuple[ProviderOperation, ...]:
    """Load canonical paid-provider attempts for one completed API request."""

    session_factory = create_session_factory(engine)
    with session_factory() as session:
        return tuple(
            SqlAlchemyProviderOperationRepository(session).list_by_correlation_id(
                context,
                research_request_id,
            )
        )


def assert_accounted_query_generation_and_support(
    engine: Engine,
    *,
    context: WorkspaceContext,
    research_request_id: UUID,
) -> None:
    """Require successful query, generation, and independent support accounting."""

    operations = load_provider_operations(
        engine,
        context=context,
        research_request_id=research_request_id,
    )
    assert {operation.operation for operation in operations} == {
        ProviderOperationKind.QUERY_EMBEDDING,
        ProviderOperationKind.ANSWER_GENERATION,
        ProviderOperationKind.STATEMENT_SUPPORT,
    }
    assert all(operation.state is ProviderOperationState.SUCCEEDED for operation in operations)
    generation = next(
        operation
        for operation in operations
        if operation.operation is ProviderOperationKind.ANSWER_GENERATION
    )
    assert generation.input_units == 120
    assert generation.output_units == 30
    assert generation.actual_cost_micro_usd == 600
    assert len(generation.trace_id) == 32
    support = next(
        operation
        for operation in operations
        if operation.operation is ProviderOperationKind.STATEMENT_SUPPORT
    )
    assert support.input_units == 90
    assert support.output_units == 10
    assert support.actual_cost_micro_usd == 300
    assert len(support.trace_id) == 32


def test_m1_api_selects_exact_lexical_evidence_in_dual_lane_mode(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = make_searchable_source(
        label="M1 API Lexical Acceptance",
        primary_text=(
            f"{M0_0_FIXTURE_ANSWER_CASE.question} {M0_0_FIXTURE_ANSWER_CASE.evidence_text}"
        ),
    )
    persist_research_source(integration_database_engine, source)
    fakes = install_provider_fakes(
        monkeypatch,
        hits=(),
        statement=M0_0_FIXTURE_ANSWER_CASE.statement,
    )
    application = create_app(make_m1_api_settings(integration_api_settings, tmp_path / "objects"))
    application.dependency_overrides[get_workspace_context] = lambda: source.context

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/research-requests",
            json={
                "question": M0_0_FIXTURE_ANSWER_CASE.question,
                "context": {"document_ids": [str(source.document.id)]},
            },
        )

    events = parse_server_sent_events(response.text)
    request_id = UUID(events[0]["payload"]["research_request_id"])
    run, candidates = load_retrieval_result(
        integration_database_engine,
        workspace_id=source.context.workspace_id,
        research_request_id=request_id,
    )
    assert response.status_code == 200
    assert [event["event_name"] for event in events] == [
        "answer.statement",
        "answer.citation",
        "answer.completed",
    ]
    assert run.mode is RetrievalMode.DUAL_LANE
    assert [
        (candidate.origin, candidate.chunk_id, candidate.selection_rank) for candidate in candidates
    ] == [(RetrievalOrigin.LEXICAL, source.chunks[0].id, 1)]
    assert len(fakes.embedding.requests) == len(fakes.dense.queries) == 1
    assert len(fakes.generator.requests) == len(fakes.support.requests) == 1
    assert events[0]["payload"]["text"] == M0_0_FIXTURE_ANSWER_CASE.statement
    assert_accounted_query_generation_and_support(
        integration_database_engine,
        context=source.context,
        research_request_id=request_id,
    )
    assert_provider_fakes_closed(fakes)


def test_m1_api_selects_only_canonical_dense_evidence_for_semantic_question(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = make_searchable_source(
        label="M1 API Semantic Acceptance",
        primary_text=SEMANTIC_PASSAGE,
        pasted_text=True,
    )
    foreign = make_searchable_source(
        label="M1 API Foreign Dense Injection",
        primary_text="Foreign workspace content must never become visible.",
        pasted_text=True,
    )
    persist_research_source(integration_database_engine, source)
    persist_research_source(integration_database_engine, foreign)
    statement = "Northstar's rollout is constrained by a shortage of high-voltage transformers."
    fakes = install_provider_fakes(
        monkeypatch,
        hits=(
            DenseRetrievalHit(chunk_id=source.chunks[0].id, raw_score=0.93),
            DenseRetrievalHit(chunk_id=foreign.chunks[0].id, raw_score=0.92),
            DenseRetrievalHit(chunk_id=uuid4(), raw_score=0.91),
        ),
        statement=statement,
    )
    application = create_app(make_m1_api_settings(integration_api_settings, tmp_path / "objects"))
    application.dependency_overrides[get_workspace_context] = lambda: source.context

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/research-requests",
            json={
                "question": SEMANTIC_QUESTION,
                "context": {"document_ids": [str(source.document.id)]},
            },
        )

    events = parse_server_sent_events(response.text)
    request_id = UUID(events[0]["payload"]["research_request_id"])
    run, candidates = load_retrieval_result(
        integration_database_engine,
        workspace_id=source.context.workspace_id,
        research_request_id=request_id,
    )
    assert response.status_code == 200
    assert [event["event_name"] for event in events] == [
        "answer.statement",
        "answer.citation",
        "answer.completed",
    ]
    assert run.mode is RetrievalMode.DUAL_LANE
    assert [
        (candidate.origin, candidate.chunk_id, candidate.selection_rank) for candidate in candidates
    ] == [(RetrievalOrigin.DENSE, source.chunks[0].id, 1)]
    assert len(fakes.embedding.requests) == len(fakes.dense.queries) == 1
    assert fakes.embedding.requests[0].inputs[0].text == SEMANTIC_QUESTION
    assert fakes.dense.queries[0].filters.document_ids == (source.document.id,)
    assert len(fakes.generator.requests) == len(fakes.support.requests) == 1
    assert events[0]["payload"]["text"] == statement
    assert_accounted_query_generation_and_support(
        integration_database_engine,
        context=source.context,
        research_request_id=request_id,
    )
    assert_provider_fakes_closed(fakes)


def test_m1_api_keeps_unanswerable_question_explicitly_insufficient(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = make_searchable_source(
        label="M1 API Insufficient Acceptance",
        primary_text="Northstar reported no information about the requested Zephyr transaction.",
    )
    persist_research_source(integration_database_engine, source)
    fakes = install_provider_fakes(monkeypatch, hits=(), statement=None)
    application = create_app(make_m1_api_settings(integration_api_settings, tmp_path / "objects"))
    application.dependency_overrides[get_workspace_context] = lambda: source.context

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/research-requests",
            json={
                "question": "What was the price of the Zephyr-991 acquisition?",
                "context": {"document_ids": [str(source.document.id)]},
            },
        )

    events = parse_server_sent_events(response.text)
    assert response.status_code == 200
    assert [event["event_name"] for event in events] == ["answer.insufficient"]
    assert len(fakes.embedding.requests) == len(fakes.dense.queries) == 1
    assert len(fakes.generator.requests) == 1
    assert fakes.generator.requests[0].sources == ()
    assert fakes.support.requests == []
    assert_provider_fakes_closed(fakes)


def test_m1_api_emits_no_generated_content_when_support_is_rejected(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = make_searchable_source(
        label="M1 API Support Rejection",
        primary_text=(
            f"{M0_0_FIXTURE_ANSWER_CASE.question} {M0_0_FIXTURE_ANSWER_CASE.evidence_text}"
        ),
    )
    persist_research_source(integration_database_engine, source)
    fakes = install_provider_fakes(
        monkeypatch,
        hits=(),
        statement=M0_0_FIXTURE_ANSWER_CASE.statement,
        supported=False,
    )
    application = create_app(make_m1_api_settings(integration_api_settings, tmp_path / "objects"))
    application.dependency_overrides[get_workspace_context] = lambda: source.context

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/research-requests",
            json={
                "question": M0_0_FIXTURE_ANSWER_CASE.question,
                "context": {"document_ids": [str(source.document.id)]},
            },
        )

    events = parse_server_sent_events(response.text)
    assert response.status_code == 200
    assert [event["event_name"] for event in events] == ["research.failed"]
    assert events[0]["payload"]["error"] == {
        "code": "EVIDENCE_VALIDATION_FAILED",
        "message": "answer failed evidence validation",
        "request_id": None,
    }
    assert events[0]["payload"]["retriable"] is False
    assert len(fakes.generator.requests) == len(fakes.support.requests) == 1
    assert_provider_fakes_closed(fakes)
