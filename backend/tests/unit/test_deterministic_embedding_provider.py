"""Unit tests for the credential-free deterministic embedding adapter."""

from math import nan
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.infrastructure.search.deterministic import (
    DeterministicEmbeddingCase,
    DeterministicEmbeddingProvider,
)
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    EmbeddingInput,
    EmbeddingPurpose,
    EmbeddingRequest,
)
from investment_research.modules.search.ports import SearchProviderConfigurationError

EMBEDDING_CONFIG_VERSION = "openai-text-embedding-3-small-1536-v1"
SHARED_TEXT = "A transformer shortage constrains customer deployments."
QUERY_VECTOR = (1.0,) + (0.0,) * (M1_EMBEDDING_DIMENSIONS - 1)
DOCUMENT_VECTOR = (0.0, 1.0) + (0.0,) * (M1_EMBEDDING_DIMENSIONS - 2)


def make_provider() -> DeterministicEmbeddingProvider:
    """Build a provider whose same text has purpose-specific fixed vectors."""

    return DeterministicEmbeddingProvider(
        embedding_config_version=EMBEDDING_CONFIG_VERSION,
        cases=(
            DeterministicEmbeddingCase(
                purpose=EmbeddingPurpose.QUERY,
                text=SHARED_TEXT,
                values=QUERY_VECTOR,
            ),
            DeterministicEmbeddingCase(
                purpose=EmbeddingPurpose.DOCUMENT,
                text=SHARED_TEXT,
                values=DOCUMENT_VECTOR,
            ),
        ),
    )


def test_embed_preserves_order_ids_version_and_purpose_specific_vectors() -> None:
    provider = make_provider()
    query_id = UUID(int=1)
    document_id = UUID(int=2)
    request = EmbeddingRequest(
        inputs=(
            EmbeddingInput(
                input_id=query_id,
                text=SHARED_TEXT,
                purpose=EmbeddingPurpose.QUERY,
            ),
            EmbeddingInput(
                input_id=document_id,
                text=SHARED_TEXT,
                purpose=EmbeddingPurpose.DOCUMENT,
            ),
        ),
        embedding_config_version=EMBEDDING_CONFIG_VERSION,
    )

    result = provider.embed(request)

    assert tuple(vector.input_id for vector in result.vectors) == (query_id, document_id)
    assert result.vectors[0].values == QUERY_VECTOR
    assert result.vectors[1].values == DOCUMENT_VECTOR
    assert all(
        vector.embedding_config_version == EMBEDDING_CONFIG_VERSION for vector in result.vectors
    )
    assert result.usage.input_units == 0
    assert result.usage.output_units == 0


def test_embed_rejects_unknown_input_without_fabricating_a_vector() -> None:
    provider = make_provider()
    request = EmbeddingRequest(
        inputs=(
            EmbeddingInput(
                input_id=UUID(int=3),
                text="An unconfigured input.",
                purpose=EmbeddingPurpose.QUERY,
            ),
        ),
        embedding_config_version=EMBEDDING_CONFIG_VERSION,
    )

    with pytest.raises(SearchProviderConfigurationError, match="not configured"):
        provider.embed(request)


def test_embed_rejects_configuration_version_drift() -> None:
    provider = make_provider()
    request = EmbeddingRequest(
        inputs=(
            EmbeddingInput(
                input_id=UUID(int=4),
                text=SHARED_TEXT,
                purpose=EmbeddingPurpose.QUERY,
            ),
        ),
        embedding_config_version="different-embedding-v2",
    )

    with pytest.raises(SearchProviderConfigurationError, match="version"):
        provider.embed(request)


def test_provider_rejects_empty_duplicate_and_invalid_case_configuration() -> None:
    with pytest.raises(ValueError, match="at least one"):
        DeterministicEmbeddingProvider(
            embedding_config_version=EMBEDDING_CONFIG_VERSION,
            cases=(),
        )

    valid_case = DeterministicEmbeddingCase(
        purpose=EmbeddingPurpose.QUERY,
        text=SHARED_TEXT,
        values=QUERY_VECTOR,
    )
    with pytest.raises(ValueError, match="unique"):
        DeterministicEmbeddingProvider(
            embedding_config_version=EMBEDDING_CONFIG_VERSION,
            cases=(valid_case, valid_case),
        )
    with pytest.raises(ValueError, match="1536"):
        DeterministicEmbeddingCase(
            purpose=EmbeddingPurpose.QUERY,
            text=SHARED_TEXT,
            values=(1.0,),
        )
    with pytest.raises(ValueError, match="finite"):
        DeterministicEmbeddingCase(
            purpose=EmbeddingPurpose.QUERY,
            text=SHARED_TEXT,
            values=(nan,) + (0.0,) * (M1_EMBEDDING_DIMENSIONS - 1),
        )
    with pytest.raises(ValidationError):
        DeterministicEmbeddingProvider(
            embedding_config_version="invalid version",
            cases=(valid_case,),
        )
