"""Opt-in, retry-free Pinecone inference over already-qualified canonical passages."""

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic
from typing import Literal

from pinecone import AsyncPinecone, RetryConfig
from pinecone.errors import (
    PineconeConnectionError,
    PineconeError,
    PineconeTimeoutError,
    RateLimitError,
    RateLimitException,
    ServiceError,
)
from pinecone.errors.exceptions import ApiError, ResponseParsingError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from investment_research.modules.operations.domain import ProviderUsage
from investment_research.modules.search.contracts import (
    RerankRequest,
    RerankResponse,
    RerankResult,
)
from investment_research.modules.search.ports import (
    SearchProviderConfigurationError,
    SearchProviderContractError,
    SearchProviderTransientError,
)
from investment_research.modules.search.reranking_contracts import (
    BGE_RERANK_CANDIDATE_LIMIT,
    BGE_RERANK_CONFIG_VERSION,
    BGE_RERANK_MODEL,
    BGE_RERANK_PASSAGE_CHARACTER_LIMIT,
    BGE_RERANK_QUERY_CHARACTER_LIMIT,
    BGE_RERANK_TOTAL_CHARACTER_LIMIT,
    require_bge_input_bounds,
)

PINECONE_RERANK_MODEL = BGE_RERANK_MODEL
PINECONE_RERANK_CONFIG_VERSION = BGE_RERANK_CONFIG_VERSION
PINECONE_RERANK_CANDIDATE_LIMIT = BGE_RERANK_CANDIDATE_LIMIT
PINECONE_RERANK_QUERY_CHARACTER_LIMIT = BGE_RERANK_QUERY_CHARACTER_LIMIT
PINECONE_RERANK_PASSAGE_CHARACTER_LIMIT = BGE_RERANK_PASSAGE_CHARACTER_LIMIT
PINECONE_RERANK_TOTAL_CHARACTER_LIMIT = BGE_RERANK_TOTAL_CHARACTER_LIMIT
PINECONE_RERANK_HOST = "https://api.pinecone.io"
PINECONE_RERANK_TIMEOUT_SECONDS = 20.0

_TRANSIENT_ERRORS = (
    TimeoutError,
    PineconeConnectionError,
    PineconeTimeoutError,
    RateLimitError,
    RateLimitException,
    ServiceError,
)


class _ProviderContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class _ProviderItem(_ProviderContract):
    index: int = Field(ge=0)
    score: float = Field(allow_inf_nan=False)
    document: None = None


class _ProviderUsage(_ProviderContract):
    rerank_units: int = Field(ge=1, le=9_223_372_036_854_775_807)


class _ProviderResponse(_ProviderContract):
    model: Literal["bge-reranker-v2-m3"]
    data: list[_ProviderItem] = Field(min_length=1, max_length=PINECONE_RERANK_CANDIDATE_LIMIT)
    usage: _ProviderUsage


def _reject_unsafe_sdk_overrides() -> None:
    """Prevent SDK environment overrides from logging content or injecting headers."""

    if os.environ.get("PINECONE_DEBUG_CURL") or os.environ.get("PINECONE_ADDITIONAL_HEADERS"):
        raise SearchProviderConfigurationError("reranking SDK overrides are not permitted")


@dataclass(frozen=True, slots=True)
class PineconeReranker:
    """Synchronously expose one cancellable async SDK operation, without nested retries.

    Call from the synchronous application execution thread, not an active event loop.
    Each call owns a fresh client and loop; a client is never shared between loops.
    Eligibility and public/synthetic-data approval belong to the caller before this boundary.
    """

    client_factory: Callable[[], AsyncPinecone] = field(repr=False)
    operation_timeout_seconds: float = PINECONE_RERANK_TIMEOUT_SECONDS
    _inflight: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not 0 < self.operation_timeout_seconds <= PINECONE_RERANK_TIMEOUT_SECONDS:
            raise ValueError("reranking timeout must be between 0 and 20 seconds")

    def rerank(self, request: RerankRequest) -> RerankResponse:
        """Rank every supplied candidate, or fail without dropping/truncating inputs."""

        self._validate_request(request)
        _reject_unsafe_sdk_overrides()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise SearchProviderConfigurationError("reranking requires synchronous execution")

        if not self._inflight.acquire(blocking=False):
            raise SearchProviderTransientError("reranking operation is already in progress")
        try:
            return asyncio.run(self._rerank_once(request))
        except ResponseParsingError:
            raise SearchProviderContractError(
                "reranking provider returned malformed output"
            ) from None
        except _TRANSIENT_ERRORS:
            raise SearchProviderTransientError(
                "reranking provider is temporarily unavailable"
            ) from None
        except ApiError as error:
            if error.status_code in {408, 409, 429} or error.status_code >= 500:
                raise SearchProviderTransientError(
                    "reranking provider is temporarily unavailable"
                ) from None
            raise SearchProviderConfigurationError(
                "reranking provider rejected the request"
            ) from None
        except PineconeError:
            raise SearchProviderConfigurationError(
                "reranking provider rejected the request"
            ) from None
        finally:
            self._inflight.release()

    @staticmethod
    def _validate_request(request: RerankRequest) -> None:
        if request.reranker_config_version != PINECONE_RERANK_CONFIG_VERSION:
            raise SearchProviderConfigurationError("unsupported reranking configuration version")
        try:
            require_bge_input_bounds(request)
        except ValueError:
            raise SearchProviderContractError(
                "reranking input exceeds the configured limits"
            ) from None

    async def _rerank_once(self, request: RerankRequest) -> RerankResponse:
        deadline = monotonic() + self.operation_timeout_seconds
        async with asyncio.timeout(self.operation_timeout_seconds):
            async with self.client_factory() as client:
                response = await client.inference.rerank(
                    model=PINECONE_RERANK_MODEL,
                    query=request.normalized_query,
                    documents=[candidate.text for candidate in request.candidates],
                    rank_fields=["text"],
                    top_n=len(request.candidates),
                    return_documents=False,
                    parameters={"truncate": "NONE"},
                )
            try:
                parsed = _ProviderResponse.model_validate(response.to_dict())
            except ValidationError:
                raise SearchProviderContractError(
                    "reranking provider returned malformed output"
                ) from None
            if sorted(item.index for item in parsed.data) != list(range(len(request.candidates))):
                raise SearchProviderContractError(
                    "reranking provider returned incomplete positions"
                )
            ordered = sorted(parsed.data, key=lambda item: (-item.score, item.index))
            result = RerankResponse(
                results=tuple(
                    RerankResult(
                        retrieval_candidate_id=request.candidates[
                            item.index
                        ].retrieval_candidate_id,
                        rank=rank,
                        score=item.score,
                    )
                    for rank, item in enumerate(ordered, start=1)
                ),
                usage=ProviderUsage(input_units=parsed.usage.rerank_units, output_units=0),
                model=parsed.model,
                reranker_config_version=request.reranker_config_version,
            )
            # Also reject a late success if synchronous decoding consumed the remaining budget.
            if monotonic() >= deadline:
                raise TimeoutError
            return result


def create_pinecone_reranker(
    *,
    api_key: SecretStr,
    operation_timeout_seconds: float = PINECONE_RERANK_TIMEOUT_SECONDS,
) -> PineconeReranker:
    """Keep the inference credential behind a fixed TLS endpoint and retry-free factory."""

    if not api_key.get_secret_value().strip():
        raise SearchProviderConfigurationError("reranking credential must not be empty")
    _reject_unsafe_sdk_overrides()

    def create_client() -> AsyncPinecone:
        return AsyncPinecone(
            api_key=api_key.get_secret_value(),
            host=PINECONE_RERANK_HOST,
            ssl_verify=True,
            timeout=operation_timeout_seconds,
            connection_pool_maxsize=1,
            retry_config=RetryConfig(max_retries=0),
        )

    return PineconeReranker(
        client_factory=create_client,
        operation_timeout_seconds=operation_timeout_seconds,
    )
