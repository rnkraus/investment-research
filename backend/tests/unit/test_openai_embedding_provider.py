"""Unit tests for the bounded OpenAI embedding adapter."""

from dataclasses import dataclass, field
from math import nan
from typing import cast
from uuid import UUID

import pytest
from billiard.exceptions import SoftTimeLimitExceeded  # type: ignore[import-untyped]
from httpx2 import Request, Response
from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from openai.types import CreateEmbeddingResponse, Embedding
from openai.types.create_embedding_response import Usage

from investment_research.infrastructure.search import openai as openai_module
from investment_research.infrastructure.search.openai import (
    OPENAI_EMBEDDING_MODEL,
    OPENAI_EMBEDDING_TIMEOUT_SECONDS,
    OpenAIEmbeddingProvider,
    OpenAIEmbeddingProviderConfigurationError,
    create_openai_embedding_provider,
)
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    EmbeddingInput,
    EmbeddingPurpose,
    EmbeddingRequest,
)
from investment_research.modules.search.ports import (
    SearchProviderConfigurationError,
    SearchProviderContractError,
    SearchProviderTransientError,
)

EMBEDDING_CONFIG_VERSION = "openai-text-embedding-3-small-1536-v1"
FIRST_ID = UUID("10000000-0000-4000-8000-000000000001")
SECOND_ID = UUID("10000000-0000-4000-8000-000000000002")
FIRST_TEXT = "First canonical passage."
SECOND_TEXT = "What does the second passage mean?"
FIRST_VECTOR = (1.0,) + (0.0,) * (M1_EMBEDDING_DIMENSIONS - 1)
SECOND_VECTOR = (0.0, 1.0) + (0.0,) * (M1_EMBEDDING_DIMENSIONS - 2)


def make_response(
    *,
    vectors: tuple[tuple[float, ...], ...] = (FIRST_VECTOR, SECOND_VECTOR),
    indices: tuple[int, ...] = (0, 1),
    model: str = OPENAI_EMBEDDING_MODEL,
    prompt_tokens: int = 7,
    total_tokens: int = 7,
) -> CreateEmbeddingResponse:
    """Build the concrete SDK response shape consumed by the adapter."""

    return CreateEmbeddingResponse.model_construct(
        data=[
            Embedding.model_construct(
                embedding=list(vector),
                index=index,
                object="embedding",
            )
            for vector, index in zip(vectors, indices, strict=True)
        ],
        model=model,
        object="list",
        usage=Usage.model_construct(
            prompt_tokens=prompt_tokens,
            total_tokens=total_tokens,
        ),
    )


@dataclass(slots=True)
class RecordingEmbeddingsResource:
    """Record one SDK call or raise one configured provider/application error."""

    response: CreateEmbeddingResponse = field(default_factory=make_response)
    error: BaseException | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def create(self, **kwargs: object) -> CreateEmbeddingResponse:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


@dataclass(slots=True)
class RecordingOpenAIClient:
    """Expose only the SDK surface used by the adapter and record shutdown."""

    embeddings: RecordingEmbeddingsResource = field(default_factory=RecordingEmbeddingsResource)
    closed: bool = False

    def close(self) -> None:
        self.closed = True


def make_request() -> EmbeddingRequest:
    """Build one mixed-purpose request whose order must be retained."""

    return EmbeddingRequest(
        inputs=(
            EmbeddingInput(
                input_id=FIRST_ID,
                text=FIRST_TEXT,
                purpose=EmbeddingPurpose.DOCUMENT,
            ),
            EmbeddingInput(
                input_id=SECOND_ID,
                text=SECOND_TEXT,
                purpose=EmbeddingPurpose.QUERY,
            ),
        ),
        embedding_config_version=EMBEDDING_CONFIG_VERSION,
    )


def make_provider(client: RecordingOpenAIClient) -> OpenAIEmbeddingProvider:
    """Inject a recording client through the concrete SDK boundary."""

    return OpenAIEmbeddingProvider(
        client=cast(OpenAI, client),
        embedding_config_version=EMBEDDING_CONFIG_VERSION,
    )


def make_status_response(status_code: int) -> Response:
    """Build the HTTP response required by concrete SDK status errors."""

    return Response(
        status_code,
        request=Request("POST", "https://api.openai.com/v1/embeddings"),
    )


def test_embed_sends_one_bounded_request_and_preserves_correlation() -> None:
    client = RecordingOpenAIClient()
    provider = make_provider(client)

    result = provider.embed(make_request())

    assert client.embeddings.calls == [
        {
            "input": [FIRST_TEXT, SECOND_TEXT],
            "model": OPENAI_EMBEDDING_MODEL,
            "dimensions": M1_EMBEDDING_DIMENSIONS,
            "encoding_format": "float",
            "timeout": OPENAI_EMBEDDING_TIMEOUT_SECONDS,
        }
    ]
    assert tuple(vector.input_id for vector in result.vectors) == (FIRST_ID, SECOND_ID)
    assert result.vectors[0].values == FIRST_VECTOR
    assert result.vectors[1].values == SECOND_VECTOR
    assert all(
        vector.embedding_config_version == EMBEDDING_CONFIG_VERSION for vector in result.vectors
    )
    assert result.usage.input_units == 7
    assert result.usage.output_units == 0


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(make_response(prompt_tokens=-1, total_tokens=-1), id="negative"),
        pytest.param(make_response(prompt_tokens=7, total_tokens=8), id="inconsistent-total"),
    ],
)
def test_embed_rejects_invalid_provider_usage(response: CreateEmbeddingResponse) -> None:
    """Fail closed instead of estimating missing or contradictory provider usage."""

    client = RecordingOpenAIClient(embeddings=RecordingEmbeddingsResource(response=response))

    with pytest.raises(SearchProviderContractError, match="usage"):
        make_provider(client).embed(make_request())


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(make_response(indices=(1, 0)), id="reordered"),
        pytest.param(
            make_response(
                vectors=(FIRST_VECTOR,),
                indices=(0,),
            ),
            id="missing",
        ),
        pytest.param(make_response(model="different-model"), id="wrong-model"),
    ],
)
def test_embed_rejects_provider_response_identity_drift(
    response: CreateEmbeddingResponse,
) -> None:
    client = RecordingOpenAIClient(embeddings=RecordingEmbeddingsResource(response=response))

    with pytest.raises(SearchProviderContractError, match="ordered request"):
        make_provider(client).embed(make_request())


@pytest.mark.parametrize(
    "invalid_vector",
    [
        pytest.param((1.0,), id="wrong-dimensions"),
        pytest.param(
            (nan,) + (0.0,) * (M1_EMBEDDING_DIMENSIONS - 1),
            id="not-finite",
        ),
    ],
)
def test_embed_rejects_invalid_vector_values(invalid_vector: tuple[float, ...]) -> None:
    response = make_response(
        vectors=(invalid_vector, SECOND_VECTOR),
        indices=(0, 1),
    )
    client = RecordingOpenAIClient(embeddings=RecordingEmbeddingsResource(response=response))

    with pytest.raises(SearchProviderContractError, match="invalid vector"):
        make_provider(client).embed(make_request())


@pytest.mark.parametrize(
    ("provider_error", "application_error", "safe_summary"),
    [
        pytest.param(
            APITimeoutError(request=Request("POST", "https://api.openai.com/v1/embeddings")),
            SearchProviderTransientError,
            "embedding provider request timed out",
            id="timeout",
        ),
        pytest.param(
            APIConnectionError(
                message="sensitive transport detail",
                request=Request("POST", "https://api.openai.com/v1/embeddings"),
            ),
            SearchProviderTransientError,
            "embedding provider connection failed",
            id="connection",
        ),
        pytest.param(
            APIStatusError(
                "sensitive request-timeout detail",
                response=make_status_response(408),
                body=None,
            ),
            SearchProviderTransientError,
            "embedding provider request timed out (HTTP 408)",
            id="http-timeout",
        ),
        pytest.param(
            APIStatusError(
                "sensitive conflict detail",
                response=make_status_response(409),
                body=None,
            ),
            SearchProviderTransientError,
            "embedding provider reported a temporary conflict (HTTP 409)",
            id="temporary-conflict",
        ),
        pytest.param(
            RateLimitError(
                "sensitive rate-limit detail",
                response=make_status_response(429),
                body={"code": "sensitive-unknown-code", "message": "sensitive response"},
            ),
            SearchProviderTransientError,
            "embedding provider returned HTTP 429 (rate or quota limit)",
            id="unknown-429",
        ),
        pytest.param(
            InternalServerError(
                "sensitive service detail",
                response=make_status_response(500),
                body=None,
            ),
            SearchProviderTransientError,
            "embedding provider server error (HTTP 500)",
            id="service",
        ),
        pytest.param(
            AuthenticationError(
                "sensitive authentication detail",
                response=make_status_response(401),
                body=None,
            ),
            SearchProviderConfigurationError,
            "embedding provider rejected the configured request (HTTP 401)",
            id="authentication",
        ),
        pytest.param(
            APIResponseValidationError(
                response=make_status_response(200),
                body={"sensitive": "provider detail"},
                message="sensitive schema detail",
            ),
            SearchProviderContractError,
            "embedding provider returned a malformed response",
            id="response-schema",
        ),
    ],
)
def test_provider_errors_are_translated_without_exposing_details(
    provider_error: Exception,
    application_error: type[Exception],
    safe_summary: str,
) -> None:
    client = RecordingOpenAIClient(embeddings=RecordingEmbeddingsResource(error=provider_error))

    with pytest.raises(application_error) as captured:
        make_provider(client).embed(make_request())

    assert str(captured.value) == safe_summary
    assert "sensitive" not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    ("provider_code", "safe_summary"),
    [
        pytest.param(
            "credit_balance_exhausted",
            "embedding provider credit balance exhausted (HTTP 429)",
            id="credits",
        ),
        pytest.param(
            "organization_spend_limit_exceeded",
            "embedding provider organization spend limit reached (HTTP 429)",
            id="organization-spend",
        ),
        pytest.param(
            "organization_usage_limit_exceeded",
            "embedding provider organization usage limit reached (HTTP 429)",
            id="organization-usage",
        ),
        pytest.param(
            "project_spend_limit_exceeded",
            "embedding provider project spend limit reached (HTTP 429)",
            id="project-spend",
        ),
        pytest.param(
            "insufficient_quota",
            "embedding provider quota exhausted (HTTP 429)",
            id="legacy-quota",
        ),
    ],
)
def test_embedding_quota_errors_are_not_retried_or_exposed(
    provider_code: str,
    safe_summary: str,
) -> None:
    provider_error = RateLimitError(
        "sensitive provider detail",
        response=make_status_response(429),
        body={"code": provider_code, "message": "sensitive provider response"},
    )
    client = RecordingOpenAIClient(embeddings=RecordingEmbeddingsResource(error=provider_error))

    with pytest.raises(SearchProviderConfigurationError) as captured:
        make_provider(client).embed(make_request())

    assert str(captured.value) == safe_summary
    assert captured.value.__cause__ is None
    assert len(client.embeddings.calls) == 1


@pytest.mark.parametrize("http_status", [400, 401, 403])
def test_embedding_rejection_retains_only_safe_http_status(http_status: int) -> None:
    provider_error = APIStatusError(
        "sensitive provider detail",
        response=make_status_response(http_status),
        body={"sensitive": "provider response"},
    )
    client = RecordingOpenAIClient(embeddings=RecordingEmbeddingsResource(error=provider_error))

    with pytest.raises(OpenAIEmbeddingProviderConfigurationError) as captured:
        make_provider(client).embed(make_request())

    assert captured.value.http_status == http_status
    assert "sensitive" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_application_abort_is_not_translated_or_retried() -> None:
    abort = SoftTimeLimitExceeded("synthetic worker deadline")
    client = RecordingOpenAIClient(embeddings=RecordingEmbeddingsResource(error=abort))

    with pytest.raises(SoftTimeLimitExceeded) as captured:
        make_provider(client).embed(make_request())

    assert captured.value is abort
    assert len(client.embeddings.calls) == 1


def test_configuration_version_drift_fails_before_provider_call() -> None:
    client = RecordingOpenAIClient()
    request = make_request().model_copy(
        update={"embedding_config_version": "different-embedding-v2"}
    )

    with pytest.raises(SearchProviderConfigurationError, match="version"):
        make_provider(client).embed(request)

    assert client.embeddings.calls == []


def test_factory_disables_sdk_retries_and_provider_close_releases_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    client = RecordingOpenAIClient()

    def recording_constructor(**kwargs: object) -> RecordingOpenAIClient:
        captured.update(kwargs)
        return client

    monkeypatch.setattr(openai_module, "OpenAI", recording_constructor)

    provider = create_openai_embedding_provider(
        api_key="synthetic-test-key",
        embedding_config_version=EMBEDDING_CONFIG_VERSION,
    )
    provider.close()

    assert captured == {
        "api_key": "synthetic-test-key",
        "timeout": OPENAI_EMBEDDING_TIMEOUT_SECONDS,
        "max_retries": 0,
    }
    assert client.closed is True
