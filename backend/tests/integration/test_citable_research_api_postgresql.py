"""Exercise opt-in source-ID API composition with PostgreSQL and zero-spend providers."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from openai import OpenAI
from sqlalchemy import Engine

from investment_research import bootstrap as bootstrap_module
from investment_research.api.dependencies import get_workspace_context
from investment_research.infrastructure.evidence.openai import OpenAIStatementSupportEvaluator
from investment_research.infrastructure.generation.openai_citable_units import (
    OpenAICitableUnitAnswerGenerator,
    citable_answer_profile,
)
from investment_research.infrastructure.openai_models import support_config_version
from investment_research.infrastructure.search.openai import OpenAIEmbeddingProvider
from investment_research.infrastructure.search.pinecone import PineconeDenseRetriever
from investment_research.main import create_app
from investment_research.modules.evidence.contracts import (
    ContextualStatementSupportInput,
    StatementSupportDecision,
    StatementSupportResult,
)
from investment_research.modules.operations.domain import (
    ProviderOperationKind,
    ProviderOperationState,
    ProviderUsage,
)
from investment_research.modules.research.citable_unit_contracts import CitableAnswerGenerationInput
from investment_research.modules.research.contracts import AnswerGenerationResult, ResearchModel
from investment_research.modules.search.contracts import DenseRetrievalHit
from investment_research.modules.search.fusion_contracts import RrfSelectionProfile
from investment_research.settings import ApiSettings
from tests.integration.factories import persist_research_source
from tests.integration.test_m1_dual_retrieval_api_postgresql import (
    FixedDenseRetriever,
    FixedQueryEmbeddingProvider,
    load_provider_operations,
    load_retrieval_result,
    make_m1_api_settings,
    make_searchable_source,
    parse_server_sent_events,
)
from tests.unit.test_openai_citable_unit_answer_generator import RecordingClient, RecordingResponses

HEADER = "Northstar Grid Analytics — FY2026 synthetic report"
QUOTE = "Customer deployments require high-voltage transformers."
TEXT = f"{HEADER}\n\n{QUOTE} {QUOTE}"
QUESTION = "What do customer deployments require?"


class SelectingGenerator:
    """Run the real source-ID adapter against a local SDK response, not the network."""

    def __init__(self, model: ResearchModel, *, unknown_id: bool) -> None:
        self.model = model
        self.unknown_id = unknown_id
        self.requests: list[CitableAnswerGenerationInput] = []
        self.closed = False

    def generate(self, request: CitableAnswerGenerationInput) -> AnswerGenerationResult:
        self.requests.append(request)
        matches = [unit for unit in request.catalog.units if unit.text == QUOTE]
        assert len(matches) == 2
        selected_id = (
            "u999" if self.unknown_id else f"u{request.catalog.units.index(matches[-1]) + 1}"
        )
        payload: dict[str, object] = {
            "coverage_status": "COMPLETE",
            "rendered_text": QUOTE,
            "statements": [{"text": QUOTE, "source_unit_ids": [selected_id]}],
            "missing_information": None,
        }
        client = RecordingClient(RecordingResponses(payload=payload))
        adapter = OpenAICitableUnitAnswerGenerator(
            client=cast(OpenAI, client), model=self.model.value
        )
        client.responses.overrides["model"] = self.model.value
        try:
            return adapter.generate(request)
        finally:
            adapter.close()
            assert client.closed

    def close(self) -> None:
        self.closed = True


class ContextualSupport:
    """Require original attribution context while independently accepting/rejecting a claim."""

    def __init__(self, *, supported: bool) -> None:
        self.supported = supported
        self.requests: list[ContextualStatementSupportInput] = []
        self.closed = False

    def evaluate(self, statement: str, evidence_texts: Sequence[str]) -> StatementSupportResult:
        raise AssertionError("source-ID composition must use the contextual support contract")

    def evaluate_contextual(
        self, request: ContextualStatementSupportInput
    ) -> StatementSupportResult:
        self.requests.append(request)
        assert request.statement == QUOTE
        assert len(request.evidence) == 1
        evidence = request.evidence[0]
        assert evidence.reference.exact_text == QUOTE
        assert evidence.attribution_context.text == HEADER
        assert (
            evidence.attribution_context.document_version_id
            == evidence.reference.document_version_id
        )
        return StatementSupportResult(
            decision=StatementSupportDecision(supported=self.supported),
            usage=ProviderUsage(input_units=90, output_units=10),
        )

    def close(self) -> None:
        self.closed = True


def install_citable_providers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hits: tuple[DenseRetrievalHit, ...],
    unknown_id: bool = False,
    supported: bool = True,
) -> tuple[
    FixedQueryEmbeddingProvider,
    FixedDenseRetriever,
    dict[ResearchModel, SelectingGenerator],
    dict[ResearchModel, ContextualSupport],
]:
    embedding, dense = FixedQueryEmbeddingProvider(), FixedDenseRetriever(hits)
    generators = {
        model: SelectingGenerator(model, unknown_id=unknown_id) for model in ResearchModel
    }
    evaluators = {model: ContextualSupport(supported=supported) for model in ResearchModel}

    def support_factory(**kwargs: object) -> OpenAIStatementSupportEvaluator:
        assert kwargs["include_attribution_context"] is True
        assert kwargs["include_document_attribution_context"] is True
        return cast(
            OpenAIStatementSupportEvaluator, evaluators[ResearchModel(str(kwargs["model"]))]
        )

    def answer_factory(**kwargs: object) -> OpenAICitableUnitAnswerGenerator:
        return cast(
            OpenAICitableUnitAnswerGenerator, generators[ResearchModel(str(kwargs["model"]))]
        )

    monkeypatch.setattr(
        bootstrap_module,
        "create_openai_embedding_provider",
        lambda **_: cast(OpenAIEmbeddingProvider, embedding),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "create_pinecone_dense_retriever",
        lambda **_: cast(PineconeDenseRetriever, dense),
    )
    monkeypatch.setattr(
        bootstrap_module, "create_openai_citable_unit_answer_generator", answer_factory
    )
    monkeypatch.setattr(
        bootstrap_module, "create_openai_statement_support_evaluator", support_factory
    )
    return embedding, dense, generators, evaluators


def citable_settings(settings: ApiSettings, root: Path) -> ApiSettings:
    values = make_m1_api_settings(settings, root).model_dump()
    values["research_profile"] = "m1-5-citable-units"
    return ApiSettings.model_validate(values)


@pytest.mark.parametrize("pasted_text", [False, True], ids=["pdf", "pasted-text"])
@pytest.mark.parametrize("generation_model", list(ResearchModel))
@pytest.mark.parametrize("support_model", list(ResearchModel))
def test_citable_api_resolves_selected_occurrence_with_context_and_accounting(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pasted_text: bool,
    generation_model: ResearchModel,
    support_model: ResearchModel,
) -> None:
    source = make_searchable_source(
        label="Citable API source", primary_text=TEXT, pasted_text=pasted_text
    )
    foreign = make_searchable_source(label="Foreign citable source", primary_text=TEXT)
    persist_research_source(integration_database_engine, source)
    persist_research_source(integration_database_engine, foreign)
    embedding, dense, generators, evaluators = install_citable_providers(
        monkeypatch,
        hits=(
            DenseRetrievalHit(chunk_id=foreign.chunks[0].id, raw_score=0.99),
            DenseRetrievalHit(chunk_id=source.chunks[0].id, raw_score=0.9),
        ),
    )
    application = create_app(citable_settings(integration_api_settings, tmp_path / "objects"))
    application.dependency_overrides[get_workspace_context] = lambda: source.context
    with TestClient(application) as client:
        response = client.post(
            "/api/v1/research-requests",
            json={
                "question": QUESTION,
                "generation_model": generation_model.value,
                "support_model": support_model.value,
            },
        )
        events = parse_server_sent_events(response.text)
        assert response.status_code == 200
        context_frames = [
            frame
            for frame in response.text.split("\n\n")
            if frame.startswith("event: research.context\n")
        ]
        assert len(context_frames) == 1
        selected_context = json.loads(context_frames[0].split("data: ", 1)[1])
        assert [item["document_id"] for item in selected_context["sources"]] == [
            str(source.document.id)
        ]
        assert selected_context["sources"][0]["text"] == TEXT
        assert selected_context["sources"][0]["chunk_id"] == str(source.chunks[0].id)
        assert str(foreign.document.id) not in context_frames[0]
        assert [event["event_name"] for event in events] == [
            "answer.statement",
            "answer.citation",
            "answer.completed",
        ]
        citation = events[1]["payload"]
        evidence_response = client.get(f"/api/v1/evidence-spans/{citation['evidence_span_id']}")
        assert evidence_response.status_code == 200
        payload = evidence_response.json()
        assert payload["exact_text"] == QUOTE
        assert payload["start_character"] == TEXT.rfind(QUOTE)
        assert payload["end_character"] == len(TEXT)
        assert payload["document_id"] == str(source.document.id)
        assert payload["document_version_id"] == str(source.version.id)
        assert payload["viewer_url"] == f"/sources/{citation['evidence_span_id']}"
        assert payload["locator"]["kind"] == ("TEXT_PASSAGE" if pasted_text else "PDF_PAGE")
        application.dependency_overrides[get_workspace_context] = lambda: foreign.context
        assert (
            client.get(f"/api/v1/evidence-spans/{citation['evidence_span_id']}").status_code == 404
        )

    request_id = UUID(events[0]["payload"]["research_request_id"])
    run, candidates = load_retrieval_result(
        integration_database_engine,
        workspace_id=source.context.workspace_id,
        research_request_id=request_id,
    )
    assert run.selector_config_version == RrfSelectionProfile().selector_config_version
    assert {candidate.chunk_id for candidate in candidates} == {source.chunks[0].id}
    assert len(embedding.requests) == len(dense.queries) == 1
    assert (
        len(generators[generation_model].requests) == len(evaluators[support_model].requests) == 1
    )
    assert not generators[
        next(model for model in ResearchModel if model is not generation_model)
    ].requests
    assert not evaluators[
        next(model for model in ResearchModel if model is not support_model)
    ].requests
    operations = load_provider_operations(
        integration_database_engine, context=source.context, research_request_id=request_id
    )
    assert len(operations) == 3
    assert all(operation.state is ProviderOperationState.SUCCEEDED for operation in operations)
    generation = next(
        operation
        for operation in operations
        if operation.operation is ProviderOperationKind.ANSWER_GENERATION
    )
    assert generation.model == generation_model.value
    assert (
        generation.configuration_version
        == citable_answer_profile(generation_model).generation_config_version
    )
    support = next(
        operation
        for operation in operations
        if operation.operation is ProviderOperationKind.STATEMENT_SUPPORT
    )
    assert support.model == support_model.value
    assert (
        support.configuration_version
        == f"{support_config_version(support_model)}-document-attribution-v1"
    )
    assert embedding.closed and dense.closed
    assert all(provider.closed for provider in generators.values())
    assert all(provider.closed for provider in evaluators.values())


@pytest.mark.parametrize("unknown_id", [False, True], ids=["unsupported-claim", "unknown-unit"])
def test_citable_api_does_not_publish_invalid_or_unsupported_proposals(
    integration_api_settings: ApiSettings,
    integration_database_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unknown_id: bool,
) -> None:
    source = make_searchable_source(label="Rejected citable source", primary_text=TEXT)
    persist_research_source(integration_database_engine, source)
    _, _, generators, evaluators = install_citable_providers(
        monkeypatch, hits=(), unknown_id=unknown_id, supported=False
    )
    application = create_app(citable_settings(integration_api_settings, tmp_path / "objects"))
    application.dependency_overrides[get_workspace_context] = lambda: source.context
    with TestClient(application) as client:
        response = client.post("/api/v1/research-requests", json={"question": QUESTION})
    events = parse_server_sent_events(response.text)
    assert response.status_code == 200
    assert [event["event_name"] for event in events] == ["research.failed"]
    # Selected source context is inspectable; rejected generated content is never an answer.
    assert QUOTE not in json.dumps(events)
    assert "synthetic-generation-key" not in response.text
    assert len(generators[ResearchModel.TERRA].requests) == 1
    assert len(evaluators[ResearchModel.TERRA].requests) == (0 if unknown_id else 1)
