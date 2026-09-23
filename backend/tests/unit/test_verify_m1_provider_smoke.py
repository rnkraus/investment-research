"""Unit tests for the explicit, bounded M1 live-provider smoke runner."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from investment_research.infrastructure.evidence.openai import OpenAIStatementSupportEvaluator
from investment_research.infrastructure.generation.openai import (
    OpenAIAnswerGenerator,
    OpenAIAnswerProviderConfigurationError,
    OpenAIAnswerProviderContractError,
)
from investment_research.infrastructure.search.openai import (
    OpenAIEmbeddingProvider,
    OpenAIEmbeddingProviderConfigurationError,
)
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
from investment_research.modules.research.ports import (
    AnswerGenerationProviderConfigurationError,
    AnswerGenerationProviderContractError,
    AnswerGenerationProviderOutputLimitError,
    AnswerGenerationProviderRefusalError,
    AnswerGenerationProviderTransientError,
)
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    DenseIndexWriteRequest,
    DenseIndexWriteResult,
    DenseVectorId,
    EmbeddingRequest,
    EmbeddingResult,
    EmbeddingVector,
)
from investment_research.modules.search.ports import (
    SearchProviderConfigurationError,
    SearchProviderContractError,
    SearchProviderTransientError,
)
from investment_research.settings import (
    M1_MAX_PROVIDER_MONTHLY_BUDGET_MICRO_USD,
    GenerationProviderSettings,
    WorkerSettings,
)
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

WORKFLOW_PATH = Path(__file__).resolve().parents[3] / ".github/workflows/m1-provider-smoke.yml"


def test_workflow_supplies_required_monthly_provider_budget() -> None:
    """Catch an incomplete live-smoke worker configuration before a hosted run."""

    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    smoke_step = workflow.split("- name: Run bounded M1 provider compatibility smoke", 1)[1]
    environment = smoke_step.split("        env:\n", 1)[1].split("        run:", 1)[0]

    assert (
        "          INVESTMENT_RESEARCH_PROVIDER_MONTHLY_BUDGET_MICRO_USD: "
        f'"{M1_MAX_PROVIDER_MONTHLY_BUDGET_MICRO_USD}"\n'
    ) in environment


@dataclass(slots=True)
class RecordingEmbeddingProvider:
    """Return one contract-valid vector or one private synthetic failure."""

    fail: bool = False
    error: Exception | None = None
    calls: list[EmbeddingRequest] = field(default_factory=list)
    closed: bool = False

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        self.calls.append(request)
        if self.fail:
            raise RuntimeError("sensitive embedding failure")
        if self.error is not None:
            raise self.error
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
        if self.writer.fail_delete:
            raise RuntimeError("sensitive Pinecone delete failure")
        self.writer.deleted = True


@dataclass(slots=True)
class FakeTime:
    """Advance smoke-test time without a real wait or external request."""

    current: float = 0.0

    def monotonic(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += seconds


@dataclass(slots=True)
class RecordingDenseWriter:
    """Acknowledge one vector and expose it until namespace cleanup."""

    operation_timeout_seconds: float = 20.0
    fail_upsert: bool = False
    fail_delete: bool = False
    fail_fetch: bool = False
    deleted: bool = False
    closed: bool = False
    fail_close: bool = False
    fetch_delay_seconds: float = 0.0
    clock: FakeTime | None = None
    upsert_calls: list[tuple[WorkspaceContext, DenseIndexWriteRequest]] = field(
        default_factory=list
    )
    fetch_calls: list[tuple[WorkspaceContext, tuple[DenseVectorId, ...], float | None]] = field(
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
        if self.fail_upsert:
            raise RuntimeError("sensitive Pinecone failure")
        return DenseIndexWriteResult(
            vector_ids=tuple(vector.vector_id for vector in request.vectors)
        )

    def fetch_visible(
        self,
        context: WorkspaceContext,
        vector_ids: tuple[DenseVectorId, ...],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[DenseVectorId, ...]:
        self.fetch_calls.append((context, vector_ids, timeout_seconds))
        if self.fail_fetch:
            raise RuntimeError("sensitive Pinecone fetch failure")
        if self.clock is not None:
            self.clock.current += self.fetch_delay_seconds
        return () if self.deleted else vector_ids

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("sensitive close failure")


@dataclass(slots=True)
class RecordingAnswerGenerator:
    """Return typed answerable and insufficient proposals for the approved questions."""

    fail: bool = False
    error: Exception | None = None
    calls: list[AnswerGenerationInput] = field(default_factory=list)
    closed: bool = False

    def generate(self, generation_input: AnswerGenerationInput) -> AnswerGenerationResult:
        self.calls.append(generation_input)
        if self.fail:
            raise RuntimeError("sensitive generation failure")
        if self.error is not None:
            raise self.error
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


def test_smoke_rejects_visibility_reported_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeTime()
    writer = RecordingDenseWriter(fetch_delay_seconds=31.0, clock=clock)
    context = WorkspaceContext(user_id=uuid4(), workspace_id=uuid4())
    monkeypatch.setattr(smoke_module, "monotonic", clock.monotonic)
    monkeypatch.setattr(smoke_module, "sleep", clock.sleep)

    with pytest.raises(ProviderSmokeVerificationError, match="did not become visible"):
        smoke_module._await_visible(
            cast(PineconeDenseIndexWriter, writer), context, "v1:" + "a" * 64
        )

    assert len(writer.fetch_calls) == 1
    assert writer.fetch_calls[0][2] == 30.0


def test_smoke_rejects_cleanup_reported_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeTime()
    writer = RecordingDenseWriter(fetch_delay_seconds=31.0, clock=clock)
    context = WorkspaceContext(user_id=uuid4(), workspace_id=uuid4())
    monkeypatch.setattr(smoke_module, "monotonic", clock.monotonic)
    monkeypatch.setattr(smoke_module, "sleep", clock.sleep)

    with pytest.raises(ProviderSmokeVerificationError, match="cleanup could not be verified"):
        smoke_module._delete_and_verify_namespace(
            cast(PineconeDenseIndexWriter, writer), context, "v1:" + "a" * 64
        )

    assert writer.deleted is True
    assert len(writer.fetch_calls) == 1
    assert writer.fetch_calls[0][2] == 30.0


@pytest.mark.parametrize(
    ("fail_delete", "fail_fetch", "expected_step"),
    [
        (True, False, "pinecone_delete"),
        (False, True, "pinecone_verify_absent"),
    ],
)
def test_smoke_identifies_cleanup_operation_without_provider_details(
    fail_delete: bool,
    fail_fetch: bool,
    expected_step: str,
) -> None:
    writer = RecordingDenseWriter(fail_delete=fail_delete, fail_fetch=fail_fetch)
    context = WorkspaceContext(user_id=uuid4(), workspace_id=uuid4())

    with pytest.raises(ProviderSmokeVerificationError) as captured:
        smoke_module._delete_and_verify_namespace(
            cast(PineconeDenseIndexWriter, writer), context, "v1:" + "a" * 64
        )

    assert captured.value.stage == "cleanup"
    assert captured.value.cleanup_steps == (expected_step,)
    assert "sensitive" not in str(captured.value)


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


def test_embedding_failure_is_safe_and_does_not_delete_an_unwritten_namespace(
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
    assert captured.value.stage == "embedding"
    assert writer.index.delete_calls == []
    assert writer.deleted is False
    assert writer.closed is True
    assert embedding.closed is True
    assert generator.closed is True
    assert support.closed is True


@pytest.mark.parametrize(
    ("provider_error", "expected_reason"),
    [
        (SearchProviderConfigurationError("sensitive detail"), "provider_configuration"),
        (SearchProviderTransientError("sensitive detail"), "provider_transient"),
        (SearchProviderContractError("sensitive detail"), "provider_contract"),
    ],
)
def test_smoke_classifies_embedding_failure_without_deleting_an_unwritten_namespace(
    monkeypatch: pytest.MonkeyPatch,
    provider_error: Exception,
    expected_reason: str,
) -> None:
    writer = RecordingDenseWriter(fail_delete=True)
    embedding = RecordingEmbeddingProvider(error=provider_error)
    install_provider_fakes(
        monkeypatch,
        embedding=embedding,
        writer=writer,
        generator=RecordingAnswerGenerator(),
        support=RecordingSupportEvaluator(),
    )

    with pytest.raises(ProviderSmokeVerificationError, match="failed safely") as captured:
        verify_m1_provider_smoke(
            manifest_path=APPROVED_MANIFEST,
            settings=make_settings(),
            generation_settings=make_generation_settings(),
        )

    assert captured.value.stage == "embedding"
    assert captured.value.reason == expected_reason
    assert "sensitive" not in str(captured.value)
    assert writer.index.delete_calls == []
    assert writer.closed is True
    assert embedding.closed is True


def test_smoke_retains_only_classified_embedding_http_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = RecordingDenseWriter()
    install_provider_fakes(
        monkeypatch,
        embedding=RecordingEmbeddingProvider(
            error=OpenAIEmbeddingProviderConfigurationError(http_status=403)
        ),
        writer=writer,
        generator=RecordingAnswerGenerator(),
        support=RecordingSupportEvaluator(),
    )

    with pytest.raises(ProviderSmokeVerificationError, match="failed safely") as captured:
        verify_m1_provider_smoke(
            manifest_path=APPROVED_MANIFEST,
            settings=make_settings(),
            generation_settings=make_generation_settings(),
        )

    assert captured.value.stage == "embedding"
    assert captured.value.reason == "provider_configuration"
    assert captured.value.http_status == 403
    assert writer.index.delete_calls == []


@pytest.mark.parametrize(
    ("fail_upsert", "fail_generation", "expected_stage"),
    [
        (True, False, "pinecone_upsert"),
        (False, True, "answer_generation"),
    ],
)
def test_smoke_reports_the_failing_provider_stage_without_details(
    monkeypatch: pytest.MonkeyPatch,
    fail_upsert: bool,
    fail_generation: bool,
    expected_stage: str,
) -> None:
    """Keep provider errors private while identifying the failed operation."""

    writer = RecordingDenseWriter(fail_upsert=fail_upsert)
    generator = RecordingAnswerGenerator(fail=fail_generation)
    install_provider_fakes(
        monkeypatch,
        embedding=RecordingEmbeddingProvider(),
        writer=writer,
        generator=generator,
        support=RecordingSupportEvaluator(),
    )

    with pytest.raises(ProviderSmokeVerificationError, match="failed safely") as captured:
        verify_m1_provider_smoke(
            manifest_path=APPROVED_MANIFEST,
            settings=make_settings(),
            generation_settings=make_generation_settings(),
        )

    assert captured.value.stage == expected_stage
    assert "sensitive" not in str(captured.value)
    assert writer.deleted is True


@pytest.mark.parametrize(
    ("provider_error", "expected_reason"),
    [
        (AnswerGenerationProviderConfigurationError("sensitive detail"), "provider_configuration"),
        (AnswerGenerationProviderTransientError("sensitive detail"), "provider_transient"),
        (AnswerGenerationProviderContractError("sensitive detail"), "provider_contract"),
        (AnswerGenerationProviderOutputLimitError("sensitive detail"), "provider_output_limit"),
        (AnswerGenerationProviderRefusalError("sensitive detail"), "provider_refusal"),
    ],
)
def test_smoke_classifies_generation_failure_without_provider_details(
    monkeypatch: pytest.MonkeyPatch,
    provider_error: Exception,
    expected_reason: str,
) -> None:
    """Expose only a fixed diagnostic category and still remove the test vector."""

    writer = RecordingDenseWriter()
    install_provider_fakes(
        monkeypatch,
        embedding=RecordingEmbeddingProvider(),
        writer=writer,
        generator=RecordingAnswerGenerator(error=provider_error),
        support=RecordingSupportEvaluator(),
    )

    with pytest.raises(ProviderSmokeVerificationError, match="failed safely") as captured:
        verify_m1_provider_smoke(
            manifest_path=APPROVED_MANIFEST,
            settings=make_settings(),
            generation_settings=make_generation_settings(),
        )

    assert captured.value.stage == "answer_generation"
    assert captured.value.reason == expected_reason
    assert "sensitive" not in str(captured.value)
    assert writer.deleted is True


def test_smoke_retains_only_fixed_generation_contract_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = RecordingDenseWriter()
    install_provider_fakes(
        monkeypatch,
        embedding=RecordingEmbeddingProvider(),
        writer=writer,
        generator=RecordingAnswerGenerator(
            error=OpenAIAnswerProviderContractError(detail="typed_answer_validation")
        ),
        support=RecordingSupportEvaluator(),
    )

    with pytest.raises(ProviderSmokeVerificationError, match="failed safely") as captured:
        verify_m1_provider_smoke(
            manifest_path=APPROVED_MANIFEST,
            settings=make_settings(),
            generation_settings=make_generation_settings(),
        )

    assert captured.value.stage == "answer_generation"
    assert captured.value.reason == "provider_contract"
    assert captured.value.contract_detail == "typed_answer_validation"
    assert writer.deleted is True


def test_smoke_retains_only_classified_openai_http_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = RecordingDenseWriter()
    install_provider_fakes(
        monkeypatch,
        embedding=RecordingEmbeddingProvider(),
        writer=writer,
        generator=RecordingAnswerGenerator(
            error=OpenAIAnswerProviderConfigurationError(http_status=400)
        ),
        support=RecordingSupportEvaluator(),
    )

    with pytest.raises(ProviderSmokeVerificationError, match="failed safely") as captured:
        verify_m1_provider_smoke(
            manifest_path=APPROVED_MANIFEST,
            settings=make_settings(),
            generation_settings=make_generation_settings(),
        )

    assert captured.value.stage == "answer_generation"
    assert captured.value.reason == "provider_configuration"
    assert captured.value.http_status == 400
    assert writer.deleted is True


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
    assert captured.value.stage == "answer_validation_and_support"
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
    assert captured.value.stage == "cleanup"
    assert captured.value.cleanup_steps == ("support_client_close", "pinecone_client_close")
    assert captured.value.primary_stage is None
    assert writer.deleted is True
    assert writer.closed is True
    assert embedding.closed is True
    assert generator.closed is True
    assert support.closed is True


@pytest.mark.parametrize(
    ("generation_error", "expected_reason", "expected_contract_detail"),
    [
        pytest.param(
            AnswerGenerationProviderOutputLimitError(),
            "provider_output_limit",
            None,
            id="output-limit",
        ),
        pytest.param(
            OpenAIAnswerProviderContractError(detail="missing_usage"),
            "provider_contract",
            "missing_usage",
            id="contract-detail",
        ),
    ],
)
def test_cleanup_failure_keeps_the_original_failure_classification(
    monkeypatch: pytest.MonkeyPatch,
    generation_error: Exception,
    expected_reason: str,
    expected_contract_detail: str | None,
) -> None:
    embedding = RecordingEmbeddingProvider()
    writer = RecordingDenseWriter(fail_delete=True)
    generator = RecordingAnswerGenerator(error=generation_error)
    support = RecordingSupportEvaluator()
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

    assert captured.value.cleanup_steps == ("pinecone_delete",)
    assert captured.value.primary_stage == "answer_generation"
    assert captured.value.primary_reason == expected_reason
    assert captured.value.primary_contract_detail == expected_contract_detail
    assert "sensitive" not in str(captured.value)
    assert embedding.closed is True
    assert writer.closed is True
    assert generator.closed is True
    assert support.closed is True


def test_main_reports_configuration_stage_without_exception_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep misconfigured credentials out of CI output while naming the failing phase."""

    def fail_configuration() -> WorkerSettings:
        raise ValueError("sensitive configuration detail")

    monkeypatch.setattr(smoke_module, "load_worker_settings", fail_configuration)

    result = smoke_module.main(
        [
            "--manifest",
            str(APPROVED_MANIFEST),
            "--confirm-public-synthetic",
            "--confirm-pinecone-starter",
        ]
    )

    output = capsys.readouterr()
    assert result == 1
    assert output.out == ""
    assert "stage=configuration" in output.err
    assert "sensitive" not in output.err


def test_main_reports_provider_stage_without_exception_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Print only a fixed phase label for failures after provider setup."""

    def fail_smoke(**_: object) -> None:
        raise ProviderSmokeVerificationError("sensitive provider detail", stage="pinecone_upsert")

    monkeypatch.setattr(smoke_module, "load_worker_settings", make_settings)
    monkeypatch.setattr(smoke_module, "load_generation_provider_settings", make_generation_settings)
    monkeypatch.setattr(smoke_module, "verify_m1_provider_smoke", fail_smoke)

    result = smoke_module.main(
        [
            "--manifest",
            str(APPROVED_MANIFEST),
            "--confirm-public-synthetic",
            "--confirm-pinecone-starter",
        ]
    )

    output = capsys.readouterr()
    assert result == 1
    assert output.out == ""
    assert "stage=pinecone_upsert" in output.err
    assert "reason=verification" in output.err
    assert "http_status=" not in output.err
    assert "sensitive" not in output.err


def test_main_reports_only_safe_http_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_smoke(**_: object) -> None:
        raise ProviderSmokeVerificationError(
            "sensitive provider detail",
            stage="answer_generation",
            reason="provider_configuration",
            http_status=401,
        )

    monkeypatch.setattr(smoke_module, "load_worker_settings", make_settings)
    monkeypatch.setattr(smoke_module, "load_generation_provider_settings", make_generation_settings)
    monkeypatch.setattr(smoke_module, "verify_m1_provider_smoke", fail_smoke)

    result = smoke_module.main(
        [
            "--manifest",
            str(APPROVED_MANIFEST),
            "--confirm-public-synthetic",
            "--confirm-pinecone-starter",
        ]
    )

    output = capsys.readouterr()
    assert result == 1
    assert output.out == ""
    assert "stage=answer_generation, reason=provider_configuration, http_status=401" in output.err
    assert "sensitive" not in output.err


def test_main_reports_only_fixed_contract_detail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_smoke(**_: object) -> None:
        raise ProviderSmokeVerificationError(
            "sensitive provider detail",
            stage="answer_generation",
            reason="provider_contract",
            contract_detail="typed_answer_validation",
        )

    monkeypatch.setattr(smoke_module, "load_worker_settings", make_settings)
    monkeypatch.setattr(smoke_module, "load_generation_provider_settings", make_generation_settings)
    monkeypatch.setattr(smoke_module, "verify_m1_provider_smoke", fail_smoke)

    result = smoke_module.main(
        [
            "--manifest",
            str(APPROVED_MANIFEST),
            "--confirm-public-synthetic",
            "--confirm-pinecone-starter",
        ]
    )

    output = capsys.readouterr()
    assert result == 1
    assert output.out == ""
    assert (
        "stage=answer_generation, reason=provider_contract, contract_detail=typed_answer_validation"
    ) in output.err
    assert "sensitive" not in output.err


def test_main_reports_cleanup_and_primary_failure_without_provider_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_smoke(**_: object) -> None:
        raise ProviderSmokeVerificationError(
            "sensitive provider detail",
            stage="cleanup",
            cleanup_steps=("pinecone_verify_absent", "pinecone_client_close"),
            primary_stage="answer_generation",
            primary_reason="provider_configuration",
            primary_http_status=403,
        )

    monkeypatch.setattr(smoke_module, "load_worker_settings", make_settings)
    monkeypatch.setattr(smoke_module, "load_generation_provider_settings", make_generation_settings)
    monkeypatch.setattr(smoke_module, "verify_m1_provider_smoke", fail_smoke)

    result = smoke_module.main(
        [
            "--manifest",
            str(APPROVED_MANIFEST),
            "--confirm-public-synthetic",
            "--confirm-pinecone-starter",
        ]
    )

    output = capsys.readouterr()
    assert result == 1
    assert output.out == ""
    assert "stage=cleanup, reason=verification" in output.err
    assert "cleanup_steps=pinecone_verify_absent,pinecone_client_close" in output.err
    assert "primary_stage=answer_generation, primary_reason=provider_configuration" in output.err
    assert "primary_http_status=403" in output.err
    assert "sensitive" not in output.err
