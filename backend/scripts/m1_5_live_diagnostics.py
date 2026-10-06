"""Retain allowlisted failure metadata, never exception messages or provider payloads."""

from contextlib import suppress
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from investment_research.infrastructure.generation.openai import (
    OpenAIAnswerContractDetail,
    OpenAIAnswerProviderConfigurationError,
    OpenAIAnswerProviderContractError,
)
from investment_research.infrastructure.search.openai import (
    OpenAIEmbeddingProviderConfigurationError,
)
from investment_research.modules.evidence.ports import (
    StatementSupportProviderConfigurationError,
    StatementSupportProviderContractError,
    StatementSupportProviderRefusalError,
    StatementSupportProviderTransientError,
)
from investment_research.modules.operations.domain import ProviderOperationKind
from investment_research.modules.research.ports import (
    AnswerGenerationProviderConfigurationError,
    AnswerGenerationProviderContractError,
    AnswerGenerationProviderOutputLimitError,
    AnswerGenerationProviderRefusalError,
    AnswerGenerationProviderTransientError,
)
from investment_research.modules.search.ports import (
    SearchProviderConfigurationError,
    SearchProviderContractError,
    SearchProviderTransientError,
    SearchProviderUnavailableError,
)

type ProviderFailureCategory = Literal[
    "configuration",
    "contract",
    "output_limit",
    "refusal",
    "transient",
    "unavailable",
    "usage_limit",
    "unexpected",
    "interrupted",
]


class ProviderFailureDiagnostic(BaseModel):
    """Optional fields keep older reports readable; no arbitrary strings are accepted."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    category: ProviderFailureCategory
    role: ProviderOperationKind | None = None
    http_status: int | None = Field(default=None, ge=400, lt=500)
    contract_detail: OpenAIAnswerContractDetail | None = None


def provider_failure(
    error: BaseException, *, role: ProviderOperationKind | None = None
) -> ProviderFailureDiagnostic:
    """Classify known application errors without inspecting messages, causes, or SDK bodies."""

    category: ProviderFailureCategory = "unexpected"
    if not isinstance(error, Exception):
        category = "interrupted"
    elif isinstance(error, AnswerGenerationProviderOutputLimitError):
        category = "output_limit"
    elif isinstance(
        error, AnswerGenerationProviderRefusalError | StatementSupportProviderRefusalError
    ):
        category = "refusal"
    elif isinstance(
        error,
        SearchProviderConfigurationError
        | AnswerGenerationProviderConfigurationError
        | StatementSupportProviderConfigurationError,
    ):
        category = "configuration"
    elif isinstance(
        error,
        SearchProviderContractError
        | AnswerGenerationProviderContractError
        | StatementSupportProviderContractError,
    ):
        category = "contract"
    elif isinstance(
        error,
        SearchProviderTransientError
        | AnswerGenerationProviderTransientError
        | StatementSupportProviderTransientError,
    ):
        category = "transient"
    elif isinstance(error, SearchProviderUnavailableError):
        category = "unavailable"
    status = None
    if (
        isinstance(
            error,
            OpenAIAnswerProviderConfigurationError | OpenAIEmbeddingProviderConfigurationError,
        )
        and type(error.http_status) is int
        and 400 <= error.http_status < 500
    ):
        status = error.http_status
    detail: OpenAIAnswerContractDetail | None = None
    if isinstance(error, OpenAIAnswerProviderContractError):
        with suppress(ValidationError):
            detail = TypeAdapter(OpenAIAnswerContractDetail).validate_python(error.detail)
    return ProviderFailureDiagnostic(
        category=category, role=role, http_status=status, contract_detail=detail
    )
