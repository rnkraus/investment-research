"""PostgreSQL API acceptance tests for deterministic M1 dual-lane retrieval."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from investment_research import bootstrap as bootstrap_module
from investment_research.api.dependencies import get_workspace_context
from investment_research.infrastructure.database.session import create_session_factory
from investment_research.infrastructure.generation.deterministic import (
    M0_0_FIXTURE_ANSWER_CASE,
)
from investment_research.infrastructure.search.openai import OpenAIEmbeddingProvider
from investment_research.infrastructure.search.pinecone import PineconeDenseRetriever
from investment_research.main import create_app
from investment_research.modules.documents.domain import OriginalMediaType, SourceLocatorKind
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.application import M1_PROCESSING_PROFILE
from investment_research.modules.processing.domain import ProcessingState
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    DenseRetrievalHit,
    DenseRetrievalQuery,
    EmbeddingRequest,
    EmbeddingVector,
)
from investment_research.modules.search.domain import RetrievalMode, RetrievalOrigin
from investment_research.modules.search.persistence import (
    RetrievalCandidateRow,
    RetrievalRunRow,
)
from investment_research.settings import ApiSettings, DenseProviderSettings
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


class FixedQueryEmbeddingProvider:
    """Return one deterministic query vector without provider network access."""

    def __init__(self) -> None:
        self.requests: list[EmbeddingRequest] = []
        self.closed = False

    def embed(self, request: EmbeddingRequest) -> tuple[EmbeddingVector, ...]:
        self.requests.append(request)
        return (
            EmbeddingVector(
                input_id=request.inputs[0].input_id,
                values=VECTOR_VALUES,
                embedding_config_version=request.embedding_config_version,
            ),
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


def make_m1_api_settings(settings: ApiSettings, object_storage_root: Path) -> ApiSettings:
    """Enable complete synthetic query-provider configuration for one test API."""

    return settings.model_copy(
        update={
            "object_storage_root": object_storage_root,
            "query_providers": DenseProviderSettings.model_validate(
                {
                    "embedding": {"api_key": "synthetic-query-embedding-key"},
                    "pinecone": {
                        "api_key": "synthetic-query-pinecone-key",
                        "index_host": "synthetic-index.example.svc.pinecone.io",
                    },
                }
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
) -> tuple[FixedQueryEmbeddingProvider, FixedDenseRetriever]:
    """Install process-owned deterministic provider doubles at the composition root."""

    embedding_provider = FixedQueryEmbeddingProvider()
    dense_retriever = FixedDenseRetriever(hits)
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
    return embedding_provider, dense_retriever


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
    embedding_provider, dense_retriever = install_provider_fakes(monkeypatch, hits=())
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
    assert len(embedding_provider.requests) == len(dense_retriever.queries) == 1
    assert embedding_provider.closed
    assert dense_retriever.closed


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
    embedding_provider, dense_retriever = install_provider_fakes(
        monkeypatch,
        hits=(
            DenseRetrievalHit(chunk_id=source.chunks[0].id, raw_score=0.93),
            DenseRetrievalHit(chunk_id=foreign.chunks[0].id, raw_score=0.92),
            DenseRetrievalHit(chunk_id=uuid4(), raw_score=0.91),
        ),
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
    assert [event["event_name"] for event in events] == ["answer.insufficient"]
    assert run.mode is RetrievalMode.DUAL_LANE
    assert [
        (candidate.origin, candidate.chunk_id, candidate.selection_rank) for candidate in candidates
    ] == [(RetrievalOrigin.DENSE, source.chunks[0].id, 1)]
    assert len(embedding_provider.requests) == len(dense_retriever.queries) == 1
    assert embedding_provider.requests[0].inputs[0].text == SEMANTIC_QUESTION
    assert dense_retriever.queries[0].filters.document_ids == (source.document.id,)
    assert embedding_provider.closed
    assert dense_retriever.closed
