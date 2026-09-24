"""OpenAI adapter for bounded, versioned document and query embeddings."""

from dataclasses import dataclass

from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    OpenAIError,
)
from pydantic import TypeAdapter, ValidationError

from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    EmbeddingRequest,
    EmbeddingResult,
    EmbeddingVector,
)
from investment_research.modules.search.domain import ConfigurationVersion
from investment_research.modules.search.ports import (
    SearchProviderConfigurationError,
    SearchProviderContractError,
    SearchProviderTransientError,
)

OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
OPENAI_EMBEDDING_TIMEOUT_SECONDS = 30.0
OPENAI_EMBEDDING_INPUT_MICRO_USD_PER_MILLION_TOKENS = 20_000

_CONFIGURATION_VERSION_ADAPTER = TypeAdapter(ConfigurationVersion)
_TRANSIENT_STATUS_CODES = frozenset({408, 409, 429})
_NON_RETRYABLE_429_SUMMARIES = {
    "credit_balance_exhausted": "embedding provider credit balance exhausted (HTTP 429)",
    "organization_spend_limit_exceeded": (
        "embedding provider organization spend limit reached (HTTP 429)"
    ),
    "organization_usage_limit_exceeded": (
        "embedding provider organization usage limit reached (HTTP 429)"
    ),
    "project_spend_limit_exceeded": "embedding provider project spend limit reached (HTTP 429)",
    "insufficient_quota": "embedding provider quota exhausted (HTTP 429)",
}


class OpenAIEmbeddingProviderConfigurationError(SearchProviderConfigurationError):
    """A rejected OpenAI embedding request with only its safe HTTP status retained."""

    def __init__(self, *, http_status: int) -> None:
        if not 400 <= http_status < 500:
            raise ValueError("configuration HTTP status must be a 4xx code")
        super().__init__(f"embedding provider rejected the configured request (HTTP {http_status})")
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class OpenAIEmbeddingProvider:
    """Translate one retry-free OpenAI embedding call into application contracts."""

    client: OpenAI
    embedding_config_version: str
    model: str = OPENAI_EMBEDDING_MODEL
    dimensions: int = M1_EMBEDDING_DIMENSIONS
    operation_timeout_seconds: float = OPENAI_EMBEDDING_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        """Reject adapter settings outside the frozen M1 embedding profile."""

        validated_version = _CONFIGURATION_VERSION_ADAPTER.validate_python(
            self.embedding_config_version
        )
        if validated_version != self.embedding_config_version:
            raise ValueError("embedding configuration version must already be normalized")
        if self.model != OPENAI_EMBEDDING_MODEL:
            raise ValueError(f"embedding model must be {OPENAI_EMBEDDING_MODEL}")
        if self.dimensions != M1_EMBEDDING_DIMENSIONS:
            raise ValueError(f"embedding dimensions must be {M1_EMBEDDING_DIMENSIONS}")
        if not 0 < self.operation_timeout_seconds <= OPENAI_EMBEDDING_TIMEOUT_SECONDS:
            raise ValueError("OpenAI embedding timeout must be between 0 and 30 seconds")

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        """Return strictly correlated finite vectors and usage or one safe error."""

        if request.embedding_config_version != self.embedding_config_version:
            raise SearchProviderConfigurationError(
                "embedding configuration version does not match the provider"
            )

        try:
            response = self.client.embeddings.create(
                input=[item.text for item in request.inputs],
                model=self.model,
                dimensions=self.dimensions,
                encoding_format="float",
                timeout=self.operation_timeout_seconds,
            )
        except APITimeoutError:
            raise SearchProviderTransientError("embedding provider request timed out") from None
        except APIConnectionError:
            raise SearchProviderTransientError("embedding provider connection failed") from None
        except APIResponseValidationError:
            raise SearchProviderContractError(
                "embedding provider returned a malformed response"
            ) from None
        except APIStatusError as error:
            if error.status_code == 429:
                quota_summary = _NON_RETRYABLE_429_SUMMARIES.get(error.code or "")
                if quota_summary is not None:
                    raise SearchProviderConfigurationError(quota_summary) from None
            if error.status_code in _TRANSIENT_STATUS_CODES or error.status_code >= 500:
                if error.status_code == 408:
                    summary = "embedding provider request timed out (HTTP 408)"
                elif error.status_code == 409:
                    summary = "embedding provider reported a temporary conflict (HTTP 409)"
                elif error.status_code == 429:
                    summary = "embedding provider returned HTTP 429 (rate or quota limit)"
                else:
                    summary = f"embedding provider server error (HTTP {error.status_code})"
                raise SearchProviderTransientError(summary) from None
            raise OpenAIEmbeddingProviderConfigurationError(http_status=error.status_code) from None
        except OpenAIError:
            raise SearchProviderConfigurationError(
                "embedding provider rejected the configured request"
            ) from None

        expected_indices = tuple(range(len(request.inputs)))
        response_indices = tuple(item.index for item in response.data)
        if (
            response.object != "list"
            or response.model != self.model
            or response_indices != expected_indices
            or any(item.object != "embedding" for item in response.data)
        ):
            raise SearchProviderContractError(
                "embedding provider response does not match the ordered request"
            )

        try:
            if response.usage.total_tokens != response.usage.prompt_tokens:
                raise ValueError("embedding usage totals are inconsistent")
            return EmbeddingResult(
                vectors=tuple(
                    EmbeddingVector(
                        input_id=request_item.input_id,
                        values=tuple(response_item.embedding),
                        embedding_config_version=self.embedding_config_version,
                    )
                    for request_item, response_item in zip(
                        request.inputs,
                        response.data,
                        strict=True,
                    )
                ),
                usage=ProviderUsage(
                    input_units=response.usage.prompt_tokens,
                    output_units=0,
                ),
            )
        except AttributeError, TypeError, ValidationError, ValueError:
            raise SearchProviderContractError(
                "embedding provider returned invalid vector or usage data"
            ) from None

    def close(self) -> None:
        """Release the SDK-owned HTTP client."""

        self.client.close()


def create_openai_embedding_provider(
    *,
    api_key: str,
    embedding_config_version: str,
    model: str = OPENAI_EMBEDDING_MODEL,
    dimensions: int = M1_EMBEDDING_DIMENSIONS,
    operation_timeout_seconds: float = OPENAI_EMBEDDING_TIMEOUT_SECONDS,
) -> OpenAIEmbeddingProvider:
    """Build one least-privilege synchronous client with SDK retries disabled."""

    if not api_key.strip():
        raise ValueError("OpenAI API key must not be empty")
    client = OpenAI(
        api_key=api_key,
        timeout=operation_timeout_seconds,
        max_retries=0,
    )
    return OpenAIEmbeddingProvider(
        client=client,
        embedding_config_version=embedding_config_version,
        model=model,
        dimensions=dimensions,
        operation_timeout_seconds=operation_timeout_seconds,
    )
