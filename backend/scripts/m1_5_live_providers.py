"""Budgeted provider ports for the sequential, opt-in M1.5 evaluation runner."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from investment_research.modules.evidence.contracts import (
    ContextualStatementSupportInput,
    StatementSupportInput,
    StatementSupportResult,
)
from investment_research.modules.evidence.ports import (
    ContextualStatementSupportEvaluator,
    StatementSupportEvaluator,
    StatementSupportProviderConfigurationError,
    StatementSupportProviderError,
)
from investment_research.modules.operations.domain import ProviderOperationKind, ProviderUsage
from investment_research.modules.research.citable_unit_contracts import CitableAnswerGenerationInput
from investment_research.modules.research.contracts import (
    AnswerGenerationInput,
    AnswerGenerationResult,
)
from investment_research.modules.research.ports import (
    AnswerGenerationPreflight,
    AnswerGenerationProviderError,
    AnswerGenerator,
)
from investment_research.modules.search.application import validate_embedding_response
from investment_research.modules.search.contracts import (
    EmbeddingPurpose,
    EmbeddingRequest,
    EmbeddingResult,
)
from investment_research.modules.search.ports import EmbeddingProvider, SearchProviderError
from scripts.m1_5_live_diagnostics import ProviderFailureDiagnostic, provider_failure
from scripts.m1_5_local_output import LocalEvaluationOutput
from scripts.plan_m1_5_live_baseline import (
    LiveBaselineBudget,
    LiveBaselineLimitError,
    RequestReservation,
    support_policy_for_prompt_version,
)


class LiveProviderCallError(RuntimeError):
    """A fixed evaluation failure category with no provider response or payload."""

    def __init__(self, role: ProviderOperationKind, reason: Literal["provider", "usage"]) -> None:
        super().__init__(f"M1.5 provider call failed: role={role.value}, reason={reason}")
        self.role = role
        self.reason = reason


class ProviderCallRecord(BaseModel):
    """Content-free usage and failures; REPORTED does not imply a correct answer."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    ordinal: int
    role: ProviderOperationKind
    usage_state: Literal["REPORTED", "UNKNOWN", "USAGE_REJECTED"]
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_micro_usd: int | None
    reserved_cost_micro_usd: int
    failure: ProviderFailureDiagnostic | None = None


class _ResultWithUsage(Protocol):
    @property
    def usage(self) -> ProviderUsage: ...


@dataclass(slots=True, repr=False)
class LiveProviderCalls:
    """Reserve before access and retain content-free accounting for all attempted calls."""

    budget: LiveBaselineBudget
    _records: list[ProviderCallRecord] = field(default_factory=list, init=False)

    @property
    def records(self) -> tuple[ProviderCallRecord, ...]:
        return tuple(self._records)

    def invoke[Result: _ResultWithUsage](
        self,
        role: ProviderOperationKind,
        *,
        payload_utf8_bytes: int,
        call: Callable[[], Result],
    ) -> Result:
        reservation = self.budget.reserve(role, payload_utf8_bytes=payload_utf8_bytes)
        try:
            result = call()
        except BaseException as error:
            self._record(reservation, "UNKNOWN", None, provider_failure(error, role=role))
            if isinstance(
                error,
                SearchProviderError | AnswerGenerationProviderError | StatementSupportProviderError,
            ) or not isinstance(error, Exception):
                raise
            raise LiveProviderCallError(role, "provider") from None
        try:
            self.budget.verify_usage(reservation, result.usage)
        except LiveBaselineLimitError:
            self._record(
                reservation,
                "USAGE_REJECTED",
                result.usage,
                ProviderFailureDiagnostic(category="usage_limit", role=role),
            )
            raise LiveProviderCallError(role, "usage") from None
        self._record(reservation, "REPORTED", result.usage)
        return result

    def _record(
        self,
        reservation: RequestReservation,
        state: Literal["REPORTED", "UNKNOWN", "USAGE_REJECTED"],
        usage: ProviderUsage | None,
        failure: ProviderFailureDiagnostic | None = None,
    ) -> None:
        cost: int | None = None
        if usage is not None:
            limit = next(
                item for item in self.budget.plan.role_limits if item.role is reservation.role
            )
            numerator = (
                usage.input_units * limit.input_micro_usd_per_million_tokens
                + usage.output_units * limit.output_micro_usd_per_million_tokens
            )
            cost = (numerator + 999_999) // 1_000_000
        self._records.append(
            ProviderCallRecord(
                ordinal=reservation.ordinal,
                role=reservation.role,
                usage_state=state,
                input_tokens=None if usage is None else usage.input_units,
                output_tokens=None if usage is None else usage.output_units,
                estimated_cost_micro_usd=cost,
                reserved_cost_micro_usd=reservation.estimated_cost_micro_usd,
                failure=failure,
            )
        )


@dataclass(frozen=True, slots=True, repr=False)
class BudgetedEmbeddingProvider:
    delegate: EmbeddingProvider
    calls: LiveProviderCalls

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        purposes = {item.purpose for item in request.inputs}
        if len(purposes) != 1:
            raise LiveBaselineLimitError("evaluation embedding batches require one purpose")
        purpose = next(iter(purposes))
        if purpose is EmbeddingPurpose.QUERY and len(request.inputs) != 1:
            raise LiveBaselineLimitError("evaluation query embedding requires one question")
        role = (
            ProviderOperationKind.QUERY_EMBEDDING
            if purpose is EmbeddingPurpose.QUERY
            else ProviderOperationKind.DOCUMENT_EMBEDDING
        )
        result = self.calls.invoke(
            role,
            payload_utf8_bytes=sum(len(item.text.encode("utf-8")) for item in request.inputs),
            call=lambda: self.delegate.embed(request),
        )
        return validate_embedding_response(request, result)


@dataclass(frozen=True, slots=True, repr=False)
class BudgetedAnswerGenerator:
    delegate: AnswerGenerator
    calls: LiveProviderCalls

    def preflight(self, generation_input: AnswerGenerationInput) -> None:
        """Keep local schema preparation outside both paid accounting layers."""

        if isinstance(self.delegate, AnswerGenerationPreflight):
            self.delegate.preflight(generation_input)

    def generate(self, generation_input: AnswerGenerationInput) -> AnswerGenerationResult:
        self.preflight(generation_input)
        # The catalog duplicates server-only text. The envelope already covers unit IDs
        # and schema metadata; count original text and question only for this profile.
        payload_bytes = (
            len(generation_input.question.encode("utf-8"))
            + sum(len(source.text.encode("utf-8")) for source in generation_input.sources)
            if isinstance(generation_input, CitableAnswerGenerationInput)
            else len(generation_input.model_dump_json().encode("utf-8"))
        )
        return self.calls.invoke(
            ProviderOperationKind.ANSWER_GENERATION,
            payload_utf8_bytes=payload_bytes,
            call=lambda: self.delegate.generate(generation_input),
        )


@dataclass(frozen=True, slots=True, repr=False)
class BudgetedSupportEvaluator:
    delegate: StatementSupportEvaluator
    calls: LiveProviderCalls
    local_output: LocalEvaluationOutput | None = None

    def evaluate(self, statement: str, evidence_texts: Sequence[str]) -> StatementSupportResult:
        if (
            support_policy_for_prompt_version(
                self.calls.budget.plan.configuration.support_prompt_version
            )
            != "m1"
        ):
            raise LiveBaselineLimitError("evaluation support policy does not match the input")
        try:
            request = StatementSupportInput(
                statement=statement, evidence_texts=tuple(evidence_texts)
            )
        except ValidationError:
            raise LiveBaselineLimitError("evaluation support input is invalid") from None
        result = self.calls.invoke(
            ProviderOperationKind.STATEMENT_SUPPORT,
            payload_utf8_bytes=len(request.model_dump_json().encode("utf-8")),
            call=lambda: self.delegate.evaluate(request.statement, request.evidence_texts),
        )
        if self.local_output is not None:
            self.local_output.support_decision(request.statement, request.evidence_texts, result)
        return result

    def evaluate_contextual(
        self, request: ContextualStatementSupportInput
    ) -> StatementSupportResult:
        """Count the complete context payload before the single opt-in provider request."""

        if (
            support_policy_for_prompt_version(
                self.calls.budget.plan.configuration.support_prompt_version
            )
            != "attribution"
        ):
            raise LiveBaselineLimitError("evaluation support policy does not match the input")
        delegate = self.delegate
        if not isinstance(delegate, ContextualStatementSupportEvaluator):
            raise StatementSupportProviderConfigurationError("contextual support is unavailable")
        try:
            request = ContextualStatementSupportInput.model_validate(request.model_dump())
        except ValidationError:
            raise LiveBaselineLimitError("evaluation contextual support input is invalid") from None
        result = self.calls.invoke(
            ProviderOperationKind.STATEMENT_SUPPORT,
            payload_utf8_bytes=len(request.model_dump_json().encode("utf-8")),
            call=lambda: delegate.evaluate_contextual(request),
        )
        if self.local_output is not None:
            self.local_output.contextual_support_decision(request, result)
        return result
