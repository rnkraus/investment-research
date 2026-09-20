"""Unit tests for the explicit, bounded M1 live-provider smoke runner."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from investment_research.infrastructure.evidence.openai import OpenAIStatementSupportEvaluator
from investment_research.infrastructure.generation.openai import OpenAIAnswerGenerator
from investment_research.infrastructure.search.openai import OpenAIEmbeddingProvider
from investment_research.infrastructure.search.pinecone import PineconeDenseIndexWriter
from investment_research.modules.evidence.contracts import (
    StatementSupportDecision,
    StatementSupportResult,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.domain import ProviderUsage
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
    DenseIndexWriteRequest,
    DenseIndexWriteResult,
    DenseVectorId,
    EmbeddingRequest,
    EmbeddingResult,
    EmbeddingVector,
)
from investment_research.settings import GenerationProviderSettings, WorkerSettings
from scripts import verify_m1_provider_smoke as smoke_module
from scripts.verify_m1_provider_smoke import (
    APPROVED_ANSWERABLE_QUESTION,
    APPROVED_MANIFEST,
    APPROVED_PASSAGE,
    APPROVED_UNANSWERABLE_QUESTION,
    MAXIMUM_TOTAL_SPEND_USD,
    ProviderSmokeVerificationError,
    verify_m1_provider_smoke,
)


@dataclass(slots=True)
class RecordingEmbeddingProvider:
    """Return one contract-valid vector or one private synthetic failure."""

    fail: bool = False
    calls: list[EmbeddingRequest] = field(default_factory=list)
    closed: bool = False

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        self.calls.append(request)
        if self.fail:
            raise RuntimeError("sensitive embedding failure")
        return EmbeddingResult(
            vectors=tuple(
                EmbeddingVector(
                    input_id=item.input_id,
                    values=(0.125,) * M1_EMBEDDING_DIMENSIONS,
                    embedding_config_version=request.embedding_config_version,
                )
                for item in request.inputs
            ),
            usage=ProviderUsage(input_units=0, output_units=0),
        )

    def close(self) -> None:
        self.closed = True


@dataclass(slots=True)
class RecordingIndex:
    """Record cleanup and make the test writer's vector disappear."""

    writer: RecordingDenseWriter
    delete_calls: list[dict[str, object]] = field(default_factory=list)

    def delete(self, **kwargs: object) -> None:
        self.delete_calls.append(kwargs)
        self.writer.deleted = True


@dataclass(slots=True)
class RecordingDenseWriter:
    """Acknowledge one vector and expose it until namespace cleanup."""

    operation_timeout_seconds: float = 20.0
    deleted: bool = False
    closed: bool = False
    fail_close: bool = False
    upsert_calls: list[tuple[WorkspaceContext, DenseIndexWriteRequest]] = field(
        default_factory=list
    )
    fetch_calls: list[tuple[WorkspaceContext, tuple[DenseVectorId, ...]]] = field(
        default_factory=list
    )
    index: RecordingIndex = field(init=False)

    def __post_init__(self) -> None:
        self.index = RecordingIndex(self)

    def upsert(
        self,
        context: WorkspaceContext,
        request: DenseIndexWriteRequest,
    ) -> DenseIndexWriteResult:
        self.upsert_calls.append((context, request))
        return DenseIndexWriteResult(
            vector_ids=tuple(vector.vector_id for vector in request.vectors)
        )

    def fetch_visible(
        self,
        context: WorkspaceContext,
        vector_ids: tuple[DenseVectorId, ...],
    ) -> tuple[DenseVectorId, ...]:
        self.fetch_calls.append((context, vector_ids))
        return () if self.deleted else vector_ids

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("sensitive close failure")


@dataclass(slots=True)
class RecordingAnswerGenerator:
    """Return typed answerable and insufficient proposals for the approved questions."""

    fail: bool = False
    calls: list[AnswerGenerationInput] = field(default_factory=list)
    closed: bool = False

    def generate(self, generation_input: AnswerGenerationInput) -> AnswerGenerationResult:
        self.calls.append(generation_input)
        if self.fail:
            raise RuntimeError("sensitive generation failure")
        if generation_input.question == APPROVED_UNANSWERABLE_QUESTION:
            return AnswerGenerationResult(
                answer=UnvalidatedGeneratedAnswer(
                    coverage_status=CoverageStatus.INSUFFICIENT,
                    rendered_text="The supplied source contains no dividend declaration.",
                    statements=(),
                    missing_information="The supplied source contains no dividend declaration.",
                ),
                usage=ProviderUsage(input_units=120, output_units=30),
            )

        source = generation_input.sources[0]
        return AnswerGenerationResult(
            answer=UnvalidatedGeneratedAnswer(
                coverage_status=CoverageStatus.COMPLETE,
                rendered_text="Untrusted provider rendering.",
                statements=(
                    UnvalidatedGeneratedStatement(
                        text=(
                            "A shortage of high-voltage transformers is the primary deployment "
                            "constraint."
                        ),
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


@dataclass(slots=True)
class RecordingSupportEvaluator:
    """Record one independent support decision and its exact evidence."""

    supported: bool = True
    fail_close: bool = False
    calls: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    closed: bool = False

    def evaluate(
        self,
        statement: str,
        evidence_texts: Sequence[str],
    ) -> StatementSupportResult:
        self.calls.append((statement, tuple(evidence_texts)))
        return StatementSupportResult(
            decision=StatementSupportDecision(supported=self.supported),
            usage=ProviderUsage(input_units=90, output_units=10),
        )

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("sensitive support close failure")


def make_settings() -> WorkerSettings:
    """Build only the synthetic provider settings needed by the smoke runner."""

    return WorkerSettings.model_validate(
        {
            "environment": "test",
            "provider_monthly_budget_micro_usd": 5_000_000,
            "indexing_providers": {
                "embedding": {"api_key": "synthetic-openai-key"},
                "pinecone": {
                    "api_key": "synthetic-pinecone-key",
                    "index_host": "synthetic-index.example.svc.pinecone.io",
                },
            },
        }
    )


def make_generation_settings() -> GenerationProviderSettings:
    """Build the synthetic generation credential used only by the smoke test."""

    return GenerationProviderSettings.model_validate({"api_key": "synthetic-generation-key"})


def install_provider_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    embedding: RecordingEmbeddingProvider,
    writer: RecordingDenseWriter,
    generator: RecordingAnswerGenerator,
    support: RecordingSupportEvaluator,
) -> None:
    """Replace all provider factories without changing the application contracts."""

    monkeypatch.setattr(
        smoke_module,
        "create_openai_embedding_provider",
        lambda **_: cast(OpenAIEmbeddingProvider, embedding),
    )
    monkeypatch.setattr(
        smoke_module,
        "create_pinecone_dense_index_writer",
        lambda **_: cast(PineconeDenseIndexWriter, writer),
    )
    monkeypatch.setattr(
        smoke_module,
        "create_openai_answer_generator",
        lambda **_: cast(OpenAIAnswerGenerator, generator),
    )
    monkeypatch.setattr(
        smoke_module,
        "create_openai_statement_support_evaluator",
        lambda **_: cast(OpenAIStatementSupportEvaluator, support),
    )


def test_smoke_uses_one_bounded_vector_and_verifies_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding = RecordingEmbeddingProvider()
    writer = RecordingDenseWriter()
    generator = RecordingAnswerGenerator()
    support = RecordingSupportEvaluator()
    install_provider_fakes(
        monkeypatch,
        embedding=embedding,
        writer=writer,
        generator=generator,
        support=support,
    )

    result = verify_m1_provider_smoke(
        manifest_path=APPROVED_MANIFEST,
        settings=make_settings(),
        generation_settings=make_generation_settings(),
    )

    assert Decimal("0") < result.estimated_maximum_spend_usd < MAXIMUM_TOTAL_SPEND_USD
    assert result.embedding_requests == 1
    assert result.generation_requests == 2
    assert result.support_evaluation_requests == 1
    assert result.pinecone_vectors == 1
    assert len(embedding.calls) == 1
    assert len(embedding.calls[0].inputs) == 1
    assert len(writer.upsert_calls) == 1
    assert len(writer.upsert_calls[0][1].vectors) == 1
    assert [request.question for request in generator.calls] == [
        APPROVED_ANSWERABLE_QUESTION,
        APPROVED_UNANSWERABLE_QUESTION,
    ]
    assert generator.calls[0].sources[0].text == APPROVED_PASSAGE
    assert len(support.calls) == 1
    assert support.calls[0][1] == (APPROVED_PASSAGE,)
    assert len(writer.index.delete_calls) == 1
    assert writer.index.delete_calls[0]["delete_all"] is True
    assert writer.index.delete_calls[0]["namespace"]
    assert writer.deleted is True
    assert writer.closed is True
    assert embedding.closed is True
    assert generator.closed is True
    assert support.closed is True


def test_smoke_rejects_unapproved_manifest_before_provider_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    constructor_calls = 0

    def record_constructor(**_: object) -> OpenAIEmbeddingProvider:
        nonlocal constructor_calls
        constructor_calls += 1
        raise AssertionError("provider construction must not be reached")

    copied_manifest = tmp_path / "manifest.yaml"
    copied_manifest.write_bytes(APPROVED_MANIFEST.read_bytes())
    monkeypatch.setattr(smoke_module, "create_openai_embedding_provider", record_constructor)

    with pytest.raises(ProviderSmokeVerificationError, match="approved M1 manifest"):
        verify_m1_provider_smoke(
            manifest_path=copied_manifest,
            settings=make_settings(),
            generation_settings=make_generation_settings(),
        )

    assert constructor_calls == 0


def test_provider_failure_is_safe_and_still_cleans_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding = RecordingEmbeddingProvider(fail=True)
    writer = RecordingDenseWriter()
    generator = RecordingAnswerGenerator()
    support = RecordingSupportEvaluator()
    install_provider_fakes(
        monkeypatch,
        embedding=embedding,
        writer=writer,
        generator=generator,
        support=support,
    )

    with pytest.raises(ProviderSmokeVerificationError, match="failed safely") as captured:
        verify_m1_provider_smoke(
            manifest_path=APPROVED_MANIFEST,
            settings=make_settings(),
            generation_settings=make_generation_settings(),
        )

    assert "sensitive" not in str(captured.value)
    assert writer.index.delete_calls[0]["delete_all"] is True
    assert writer.closed is True
    assert embedding.closed is True
    assert generator.closed is True
    assert support.closed is True


def test_unsupported_generated_statement_fails_safely_and_cleans_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding = RecordingEmbeddingProvider()
    writer = RecordingDenseWriter()
    generator = RecordingAnswerGenerator()
    support = RecordingSupportEvaluator(supported=False)
    install_provider_fakes(
        monkeypatch,
        embedding=embedding,
        writer=writer,
        generator=generator,
        support=support,
    )

    with pytest.raises(ProviderSmokeVerificationError, match="failed safely") as captured:
        verify_m1_provider_smoke(
            manifest_path=APPROVED_MANIFEST,
            settings=make_settings(),
            generation_settings=make_generation_settings(),
        )

    assert "supported" not in str(captured.value)
    assert len(generator.calls) == 1
    assert len(support.calls) == 1
    assert writer.deleted is True
    assert writer.closed is True
    assert embedding.closed is True
    assert generator.closed is True
    assert support.closed is True


def test_budget_failure_happens_before_provider_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_calls = 0

    def record_constructor(**_: object) -> OpenAIEmbeddingProvider:
        nonlocal constructor_calls
        constructor_calls += 1
        raise AssertionError("provider construction must not be reached")

    monkeypatch.setattr(smoke_module, "MAXIMUM_TOTAL_SPEND_USD", Decimal("0.000001"))
    monkeypatch.setattr(smoke_module, "create_openai_embedding_provider", record_constructor)

    with pytest.raises(ProviderSmokeVerificationError, match="spend ceiling"):
        verify_m1_provider_smoke(
            manifest_path=APPROVED_MANIFEST,
            settings=make_settings(),
            generation_settings=make_generation_settings(),
        )

    assert constructor_calls == 0


def test_cleanup_failure_is_safe_and_does_not_skip_other_client_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding = RecordingEmbeddingProvider()
    writer = RecordingDenseWriter(fail_close=True)
    generator = RecordingAnswerGenerator()
    support = RecordingSupportEvaluator(fail_close=True)
    install_provider_fakes(
        monkeypatch,
        embedding=embedding,
        writer=writer,
        generator=generator,
        support=support,
    )

    with pytest.raises(ProviderSmokeVerificationError, match="cleanup failed") as captured:
        verify_m1_provider_smoke(
            manifest_path=APPROVED_MANIFEST,
            settings=make_settings(),
            generation_settings=make_generation_settings(),
        )

    assert "sensitive" not in str(captured.value)
    assert writer.deleted is True
    assert writer.closed is True
    assert embedding.closed is True
    assert generator.closed is True
    assert support.closed is True
