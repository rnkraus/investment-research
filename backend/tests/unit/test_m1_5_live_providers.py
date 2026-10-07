"""Budgeted evaluation ports never require live credentials or network access."""

import json
import socket
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from investment_research.infrastructure.generation.openai import (
    OpenAIAnswerProviderConfigurationError,
    OpenAIAnswerProviderContractError,
)
from investment_research.infrastructure.search.openai import (
    OpenAIEmbeddingProviderConfigurationError,
)
from investment_research.modules.evidence.contracts import (
    StatementSupportDecision,
    StatementSupportResult,
)
from investment_research.modules.evidence.ports import (
    StatementSupportProviderConfigurationError,
    StatementSupportProviderContractError,
    StatementSupportProviderRefusalError,
    StatementSupportProviderTransientError,
)
from investment_research.modules.operations.domain import ProviderOperationKind, ProviderUsage
from investment_research.modules.research.citable_unit_contracts import CitableUnitLimitError
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    AnswerGenerationResult,
    UnvalidatedGeneratedAnswer,
)
from investment_research.modules.research.domain import CoverageStatus
from investment_research.modules.research.ports import AnswerGenerationProviderOutputLimitError
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    EmbeddingInput,
    EmbeddingPurpose,
    EmbeddingRequest,
    EmbeddingResult,
    EmbeddingVector,
)
from investment_research.modules.search.ports import SearchProviderTransientError
from scripts.m1_5_live_diagnostics import ProviderFailureDiagnostic, provider_failure
from scripts.m1_5_live_providers import (
    BudgetedAnswerGenerator,
    BudgetedEmbeddingProvider,
    BudgetedSupportEvaluator,
    LiveProviderCallError,
    LiveProviderCalls,
)
from scripts.m1_5_local_output import LocalEvaluationOutput
from scripts.plan_m1_5_live_baseline import LiveBaselineBudget, LiveBaselineLimitError, build_plan
from tests.unit.test_openai_citable_unit_answer_generator import make_generator, make_input

MARKER = "synthetic-private-content-marker"
CONFIG = "openai-text-embedding-3-small-1536-v1"


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("unit tests must not access a network")

    monkeypatch.setattr(socket.socket, "connect", fail)


@dataclass
class RecordingProviders:
    error: BaseException | None = None
    usage: ProviderUsage = field(
        default_factory=lambda: ProviderUsage(input_units=10, output_units=0)
    )
    requests: int = 0
    supported: bool = True
    support_inputs: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)

    def _before_call(self) -> None:
        self.requests += 1
        if self.error is not None:
            raise self.error

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        self._before_call()
        return EmbeddingResult(
            vectors=tuple(
                EmbeddingVector(
                    input_id=item.input_id,
                    values=(0.1,) * M1_EMBEDDING_DIMENSIONS,
                    embedding_config_version=request.embedding_config_version,
                )
                for item in request.inputs
            ),
            usage=self.usage,
        )

    def generate(self, generation_input: AnswerGenerationInput) -> AnswerGenerationResult:
        self._before_call()
        return AnswerGenerationResult(
            answer=UnvalidatedGeneratedAnswer(
                coverage_status=CoverageStatus.INSUFFICIENT,
                rendered_text="No evidence.",
                statements=(),
                missing_information="No evidence.",
            ),
            usage=self.usage,
        )

    def evaluate(self, statement: str, evidence_texts: Sequence[str]) -> StatementSupportResult:
        self._before_call()
        self.support_inputs.append((statement, tuple(evidence_texts)))
        return StatementSupportResult(
            decision=StatementSupportDecision(supported=self.supported), usage=self.usage
        )


def embedding_request(purpose: EmbeddingPurpose = EmbeddingPurpose.QUERY) -> EmbeddingRequest:
    return EmbeddingRequest(
        inputs=(EmbeddingInput(input_id=uuid4(), text=MARKER * 4, purpose=purpose),),
        embedding_config_version=CONFIG,
    )


def generation_input() -> AnswerGenerationInput:
    return AnswerGenerationInput(research_request_id=uuid4(), question=MARKER, sources=())


def test_citable_budget_counts_original_payload_once_and_preflights_before_reserving() -> None:
    request = make_input()
    delegate, client = make_generator(request)
    calls = LiveProviderCalls(LiveBaselineBudget(build_plan(answer_policy="citable_units")))
    BudgetedAnswerGenerator(delegate, calls).generate(request)
    record = calls.records[0]
    expected = LiveBaselineBudget(calls.budget.plan).reserve(
        ProviderOperationKind.ANSWER_GENERATION,
        payload_utf8_bytes=len(request.question.encode("utf-8"))
        + sum(len(item.text.encode("utf-8")) for item in request.sources),
    )
    assert record.reserved_cost_micro_usd == expected.estimated_cost_micro_usd
    assert len(client.responses.calls) == 1


def test_citable_preflight_failure_does_not_reserve_or_call_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = make_input()
    delegate, client = make_generator(request)

    def reject(*args: object) -> None:
        raise CitableUnitLimitError("source-unit metadata exceeds the input reservation")

    monkeypatch.setattr(type(delegate), "preflight", reject)
    calls = LiveProviderCalls(LiveBaselineBudget(build_plan(answer_policy="citable_units")))
    with pytest.raises(CitableUnitLimitError):
        BudgetedAnswerGenerator(delegate, calls).generate(request)
    assert calls.records == ()
    assert calls.budget.reserved_cost_micro_usd == 0
    assert not client.responses.calls


def test_all_four_roles_reserve_and_record_reported_usage_without_content() -> None:
    delegate = RecordingProviders()
    calls = LiveProviderCalls(LiveBaselineBudget(build_plan()))
    embedding = BudgetedEmbeddingProvider(delegate, calls)
    embedding.embed(embedding_request(EmbeddingPurpose.DOCUMENT))
    embedding.embed(embedding_request())
    generated = BudgetedAnswerGenerator(delegate, calls).generate(generation_input())
    supported = BudgetedSupportEvaluator(delegate, calls).evaluate(MARKER, (MARKER,))

    assert generated.answer.coverage_status is CoverageStatus.INSUFFICIENT
    assert supported.decision.supported is True
    assert [item.role for item in calls.records] == list(ProviderOperationKind)
    assert len(calls.records) == delegate.requests == 4
    assert all(item.usage_state == "REPORTED" and item.input_tokens == 10 for item in calls.records)
    assert all(item.estimated_cost_micro_usd is not None for item in calls.records)
    assert (
        sum(item.reserved_cost_micro_usd for item in calls.records)
        == calls.budget.reserved_cost_micro_usd
    )
    assert calls.budget.reserved_cost_micro_usd > sum(
        item.estimated_cost_micro_usd or 0 for item in calls.records
    )
    assert all(MARKER not in item.model_dump_json() for item in calls.records)


@pytest.mark.parametrize("role", list(ProviderOperationKind))
def test_budget_denial_never_dispatches_a_call(role: ProviderOperationKind) -> None:
    delegate = RecordingProviders()
    calls = LiveProviderCalls(LiveBaselineBudget(build_plan(maximum_spend_micro_usd=1)))

    with pytest.raises(LiveBaselineLimitError, match="budget"):
        if role in {
            ProviderOperationKind.DOCUMENT_EMBEDDING,
            ProviderOperationKind.QUERY_EMBEDDING,
        }:
            purpose = (
                EmbeddingPurpose.DOCUMENT
                if role is ProviderOperationKind.DOCUMENT_EMBEDDING
                else EmbeddingPurpose.QUERY
            )
            BudgetedEmbeddingProvider(delegate, calls).embed(embedding_request(purpose))
        elif role is ProviderOperationKind.ANSWER_GENERATION:
            BudgetedAnswerGenerator(delegate, calls).generate(generation_input())
        else:
            BudgetedSupportEvaluator(delegate, calls).evaluate(MARKER, (MARKER,))
    assert delegate.requests == 0
    assert not calls.records
    assert calls.budget.reserved_cost_micro_usd == 0


def test_mixed_purpose_embedding_is_rejected_before_access() -> None:
    delegate = RecordingProviders()
    calls = LiveProviderCalls(LiveBaselineBudget(build_plan()))
    document = embedding_request(EmbeddingPurpose.DOCUMENT)
    query = embedding_request()
    mixed = document.model_copy(update={"inputs": (*document.inputs, *query.inputs)})

    with pytest.raises(LiveBaselineLimitError, match="one purpose"):
        BudgetedEmbeddingProvider(delegate, calls).embed(mixed)
    assert delegate.requests == 0


def test_query_batch_cannot_hide_multiple_questions_in_one_request() -> None:
    delegate = RecordingProviders()
    calls = LiveProviderCalls(LiveBaselineBudget(build_plan()))
    query = embedding_request()
    batch = query.model_copy(update={"inputs": (*query.inputs, *embedding_request().inputs)})

    with pytest.raises(LiveBaselineLimitError, match="one question"):
        BudgetedEmbeddingProvider(delegate, calls).embed(batch)
    assert delegate.requests == 0


def test_typed_provider_failure_preserves_classification_and_unknown_usage() -> None:
    error = SearchProviderTransientError("embedding provider timed out")
    delegate = RecordingProviders(error=error)
    calls = LiveProviderCalls(LiveBaselineBudget(build_plan()))

    with pytest.raises(SearchProviderTransientError) as caught:
        BudgetedEmbeddingProvider(delegate, calls).embed(embedding_request())
    assert caught.value is error
    record = calls.records[0]
    assert record.usage_state == "UNKNOWN"
    assert record.input_tokens is record.output_tokens is record.estimated_cost_micro_usd is None
    assert record.reserved_cost_micro_usd == calls.budget.reserved_cost_micro_usd > 0


def test_unexpected_provider_failure_does_not_expose_exception_content() -> None:
    delegate = RecordingProviders(error=RuntimeError(MARKER))
    calls = LiveProviderCalls(LiveBaselineBudget(build_plan()))

    with pytest.raises(LiveProviderCallError) as caught:
        BudgetedAnswerGenerator(delegate, calls).generate(generation_input())
    assert caught.value.reason == "provider"
    assert MARKER not in str(caught.value)
    assert caught.value.__suppress_context__ is True
    assert calls.records[0].usage_state == "UNKNOWN"


def test_interruption_keeps_unknown_usage_and_the_reservation() -> None:
    delegate = RecordingProviders(error=KeyboardInterrupt())
    calls = LiveProviderCalls(LiveBaselineBudget(build_plan()))

    with pytest.raises(KeyboardInterrupt):
        BudgetedAnswerGenerator(delegate, calls).generate(generation_input())
    assert calls.records[0].usage_state == "UNKNOWN"
    assert calls.budget.reserved_cost_micro_usd > 0


def test_usage_overrun_is_recorded_and_stops_future_calls() -> None:
    delegate = RecordingProviders(usage=ProviderUsage(input_units=10, output_units=2_001))
    calls = LiveProviderCalls(LiveBaselineBudget(build_plan()))

    with pytest.raises(LiveProviderCallError) as caught:
        BudgetedAnswerGenerator(delegate, calls).generate(generation_input())
    assert caught.value.reason == "usage"
    assert calls.records[0].usage_state == "USAGE_REJECTED"
    assert calls.records[0].output_tokens == 2_001
    with pytest.raises(LiveBaselineLimitError, match="stopped"):
        BudgetedEmbeddingProvider(delegate, calls).embed(embedding_request())
    assert delegate.requests == 1


@pytest.mark.parametrize("supported", [True, False])
@pytest.mark.parametrize("show_local_output", [True, False])
def test_support_inspection_displays_exact_provider_input_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    supported: bool,
    show_local_output: bool,
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    local_output = (
        LocalEvaluationOutput(answer_model="synthetic-answer", support_model="synthetic-support")
        if show_local_output
        else None
    )
    if local_output is not None:
        local_output.case_id = "synthetic-support-inspection"
    statement = "The pilot requires transformers."
    evidence_texts = ("  Transformer shortage.\nNext line.  ", "Public café text\x1b[31m")
    delegate = RecordingProviders(supported=supported)
    calls = LiveProviderCalls(LiveBaselineBudget(build_plan()))

    result = BudgetedSupportEvaluator(delegate, calls, local_output).evaluate(
        statement, evidence_texts
    )

    assert result.decision.supported is supported
    assert delegate.support_inputs == [(statement, evidence_texts)]
    assert delegate.requests == len(calls.records) == 1
    assert all(text not in calls.records[0].model_dump_json() for text in evidence_texts)
    terminal = capsys.readouterr().out
    if not show_local_output:
        assert terminal == ""
        return
    assert "\x1b" not in terminal
    assert "\\u001b" in terminal
    assert json.loads(terminal) == {
        "event": "local.evaluation.support_decision",
        "case_id": "synthetic-support-inspection",
        "role": "support",
        "model": "synthetic-support",
        "statement": statement,
        "evidence_texts": list(evidence_texts),
        "supported": supported,
        "notice": "Application support decision, not an independent quality assessment.",
    }
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert local_output is not None
    with pytest.raises(ValueError, match="outside CI"):
        local_output.support_decision(statement, evidence_texts, result)
    assert capsys.readouterr().out == ""


def test_support_validation_happens_before_reservation_or_provider_access() -> None:
    delegate = RecordingProviders()
    calls = LiveProviderCalls(LiveBaselineBudget(build_plan()))

    with pytest.raises(LiveBaselineLimitError, match="support input is invalid") as caught:
        BudgetedSupportEvaluator(delegate, calls).evaluate(" ", (MARKER,))
    assert MARKER not in str(caught.value)
    assert caught.value.__suppress_context__ is True
    assert delegate.requests == 0
    assert not calls.records


@pytest.mark.parametrize(
    ("error", "category", "status", "detail"),
    [
        (OpenAIAnswerProviderConfigurationError(http_status=401), "configuration", 401, None),
        (OpenAIEmbeddingProviderConfigurationError(http_status=429), "configuration", 429, None),
        (
            OpenAIAnswerProviderContractError(detail="quote_not_found"),
            "contract",
            None,
            "quote_not_found",
        ),
        (
            OpenAIAnswerProviderContractError(detail="quote_ambiguous"),
            "contract",
            None,
            "quote_ambiguous",
        ),
        (AnswerGenerationProviderOutputLimitError(MARKER), "output_limit", None, None),
        (StatementSupportProviderConfigurationError(MARKER), "configuration", None, None),
        (StatementSupportProviderContractError(MARKER), "contract", None, None),
        (StatementSupportProviderRefusalError(MARKER), "refusal", None, None),
        (StatementSupportProviderTransientError(MARKER), "transient", None, None),
        (SearchProviderTransientError(MARKER), "transient", None, None),
        (RuntimeError(MARKER), "unexpected", None, None),
        (KeyboardInterrupt(MARKER), "interrupted", None, None),
    ],
)
def test_failure_metadata_is_allowlisted_and_never_contains_exception_content(
    error: BaseException, category: str, status: int | None, detail: str | None
) -> None:
    calls = LiveProviderCalls(LiveBaselineBudget(build_plan()))
    delegate = RecordingProviders(error=error)
    expected_error = LiveProviderCallError if type(error) is RuntimeError else type(error)
    with pytest.raises(expected_error):
        BudgetedAnswerGenerator(delegate, calls).generate(generation_input())
    diagnostic = calls.records[0].failure
    assert diagnostic is not None
    assert diagnostic.category == category
    assert diagnostic.role is ProviderOperationKind.ANSWER_GENERATION
    assert diagnostic.http_status == status
    assert diagnostic.contract_detail == detail
    assert MARKER not in calls.records[0].model_dump_json()


def test_mutated_adapter_details_and_arbitrary_exception_attributes_are_not_copied() -> None:
    error = OpenAIAnswerProviderContractError(detail="quote_not_found")
    error.__dict__["detail"] = MARKER
    assert provider_failure(error).contract_detail is None
    configuration = OpenAIAnswerProviderConfigurationError(http_status=401)
    configuration.__dict__["http_status"] = MARKER
    assert provider_failure(configuration).http_status is None
    unexpected = RuntimeError(MARKER)
    unexpected.__dict__.update(http_status=401, detail="quote_not_found")
    assert provider_failure(unexpected) == ProviderFailureDiagnostic(category="unexpected")


def test_usage_failure_has_its_own_diagnostic_and_success_has_none() -> None:
    calls = LiveProviderCalls(LiveBaselineBudget(build_plan()))
    delegate = RecordingProviders(usage=ProviderUsage(input_units=10, output_units=2_001))
    with pytest.raises(LiveProviderCallError):
        BudgetedAnswerGenerator(delegate, calls).generate(generation_input())
    assert calls.records[0].failure == ProviderFailureDiagnostic(
        category="usage_limit", role=ProviderOperationKind.ANSWER_GENERATION
    )
    successful = LiveProviderCalls(LiveBaselineBudget(build_plan()))
    BudgetedAnswerGenerator(RecordingProviders(), successful).generate(generation_input())
    assert successful.records[0].failure is None
