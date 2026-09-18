"""OpenAI adapter for bounded, versioned document and query embeddings."""

from dataclasses import dataclass

from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    OpenAI,
    OpenAIError,
)
from pydantic import TypeAdapter, ValidationError

from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    EmbeddingRequest,
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

_CONFIGURATION_VERSION_ADAPTER = TypeAdapter(ConfigurationVersion)
_TRANSIENT_STATUS_CODES = frozenset({408, 409, 429})


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

    def embed(self, request: EmbeddingRequest) -> tuple[EmbeddingVector, ...]:
        """Return strictly correlated finite vectors or one safe provider error."""

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
        except APIConnectionError:
            raise SearchProviderTransientError(
                "embedding provider is temporarily unavailable"
            ) from None
        except APIResponseValidationError:
            raise SearchProviderContractError(
                "embedding provider returned a malformed response"
            ) from None
        except APIStatusError as error:
            if error.status_code in _TRANSIENT_STATUS_CODES or error.status_code >= 500:
                raise SearchProviderTransientError(
                    "embedding provider is temporarily unavailable"
                ) from None
            raise SearchProviderConfigurationError(
                "embedding provider rejected the configured request"
            ) from None
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
            return tuple(
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
            )
        except ValidationError, ValueError:
            raise SearchProviderContractError(
                "embedding provider returned invalid vector data"
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
