"""Unit tests for provider-neutral search boundary contracts."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.modules.search.contracts import (
    M1_EMBEDDING_BATCH_LIMIT,
    M1_EMBEDDING_CHARACTER_LIMIT,
    M1_EMBEDDING_DIMENSIONS,
    DenseIndexVector,
    DenseIndexWriteRequest,
    DenseIndexWriteResult,
    DenseQualificationRequest,
    DenseRetrievalHit,
    DenseRetrievalQuery,
    EmbeddingInput,
    EmbeddingPurpose,
    EmbeddingRequest,
    EmbeddingVector,
    RerankCandidate,
    RerankRequest,
    RerankResult,
)
from investment_research.modules.search.domain import RetrievalFilters

INPUT_ID = UUID("00000000-0000-4000-8000-000000000301")
CANDIDATE_ID = UUID("00000000-0000-4000-8000-000000000302")
CHUNK_ID = UUID("00000000-0000-4000-8000-000000000303")
DOCUMENT_VERSION_ID = UUID("00000000-0000-4000-8000-000000000304")
CHUNK_SET_ID = UUID("00000000-0000-4000-8000-000000000305")
VECTOR_ID = f"v1:{'a' * 64}"
VECTOR_VALUES = (0.0,) * M1_EMBEDDING_DIMENSIONS


def test_embedding_contracts_preserve_correlation_and_version() -> None:
    embedding_input = EmbeddingInput(
        input_id=INPUT_ID,
        text="Grid modernization increased recurring revenue.",
        purpose=EmbeddingPurpose.DOCUMENT,
    )
    request = EmbeddingRequest(
        inputs=(embedding_input,),
        embedding_config_version="embedding-v1",
    )
    vector = EmbeddingVector(
        input_id=INPUT_ID,
        values=VECTOR_VALUES,
        embedding_config_version="embedding-v1",
    )

    assert request.inputs == (embedding_input,)
    assert vector.input_id == request.inputs[0].input_id
    assert vector.embedding_config_version == request.embedding_config_version


def test_embedding_contracts_reject_ambiguous_or_malformed_inputs() -> None:
    embedding_input = EmbeddingInput(
        input_id=INPUT_ID,
        text="Canonical source text",
        purpose=EmbeddingPurpose.QUERY,
    )

    with pytest.raises(ValidationError, match="embedding input IDs must be unique"):
        EmbeddingRequest(
            inputs=(embedding_input, embedding_input),
            embedding_config_version="embedding-v1",
        )
    with pytest.raises(ValidationError, match="non-whitespace"):
        EmbeddingInput(input_id=INPUT_ID, text="   ", purpose=EmbeddingPurpose.QUERY)
    with pytest.raises(ValidationError, match="finite"):
        EmbeddingVector(
            input_id=INPUT_ID,
            values=(float("nan"), *VECTOR_VALUES[1:]),
            embedding_config_version="embedding-v1",
        )


def test_embedding_request_enforces_m1_batch_and_character_limits() -> None:
    too_many_inputs = tuple(
        EmbeddingInput(
            input_id=UUID(int=index + 1),
            text="Canonical source text",
            purpose=EmbeddingPurpose.DOCUMENT,
        )
        for index in range(M1_EMBEDDING_BATCH_LIMIT + 1)
    )

    with pytest.raises(ValidationError):
        EmbeddingRequest(
            inputs=too_many_inputs,
            embedding_config_version="embedding-v1",
        )
    with pytest.raises(ValidationError, match="character limit"):
        EmbeddingRequest(
            inputs=(
                EmbeddingInput(
                    input_id=INPUT_ID,
                    text="x" * (M1_EMBEDDING_CHARACTER_LIMIT + 1),
                    purpose=EmbeddingPurpose.DOCUMENT,
                ),
            ),
            embedding_config_version="embedding-v1",
        )


def test_embedding_vectors_require_exact_m1_dimensions() -> None:
    with pytest.raises(ValidationError):
        EmbeddingVector(
            input_id=INPUT_ID,
            values=VECTOR_VALUES[:-1],
            embedding_config_version="embedding-v1",
        )


def test_dense_index_contracts_keep_metadata_bounded_and_coherent() -> None:
    vector = DenseIndexVector(
        vector_id=VECTOR_ID,
        values=VECTOR_VALUES,
        chunk_id=CHUNK_ID,
        document_version_id=DOCUMENT_VERSION_ID,
        chunk_set_id=CHUNK_SET_ID,
        embedding_config_version="embedding-v1",
        dense_index_config_version="dense-v1",
    )
    request = DenseIndexWriteRequest(
        vectors=(vector,),
        dense_index_config_version="dense-v1",
    )
    result = DenseIndexWriteResult(vector_ids=(VECTOR_ID,))

    assert request.vectors == (vector,)
    assert result.vector_ids == (VECTOR_ID,)
    assert "text" not in vector.model_dump()

    with pytest.raises(ValidationError, match="vector IDs must be unique"):
        DenseIndexWriteRequest(
            vectors=(vector, vector),
            dense_index_config_version="dense-v1",
        )
    with pytest.raises(ValidationError, match="configuration versions must match"):
        DenseIndexWriteRequest(
            vectors=(vector.model_copy(update={"dense_index_config_version": "dense-v2"}),),
            dense_index_config_version="dense-v1",
        )


def test_dense_retrieval_contracts_reject_non_finite_provider_values() -> None:
    with pytest.raises(ValidationError, match="finite"):
        DenseRetrievalQuery(
            query_vector=(float("inf"), *VECTOR_VALUES[1:]),
            filters=RetrievalFilters(),
            embedding_config_version="embedding-v1",
            dense_index_config_version="dense-v1",
            candidate_limit=10,
        )
    with pytest.raises(ValidationError):
        DenseRetrievalHit(chunk_id=CHUNK_ID, raw_score=float("nan"))

    provider_hit = DenseRetrievalHit(chunk_id=CHUNK_ID, raw_score=0.75)
    with pytest.raises(ValidationError, match="dense provider chunk IDs must be unique"):
        DenseQualificationRequest(
            provider_hits=(provider_hit, provider_hit),
            filters=RetrievalFilters(),
            lexical_config_version="lexical-v1",
            embedding_config_version="embedding-v1",
            dense_index_config_version="dense-v1",
        )


def test_rerank_contracts_require_unique_candidates_and_finite_scores() -> None:
    candidate = RerankCandidate(
        retrieval_candidate_id=CANDIDATE_ID,
        text="Canonical candidate text",
    )

    with pytest.raises(ValidationError, match="rerank candidate IDs must be unique"):
        RerankRequest(
            normalized_query="revenue growth",
            candidates=(candidate, candidate),
            reranker_config_version="reranker-v1",
        )
    with pytest.raises(ValidationError):
        RerankResult(
            retrieval_candidate_id=CANDIDATE_ID,
            rank=1,
            score=float("inf"),
        )
