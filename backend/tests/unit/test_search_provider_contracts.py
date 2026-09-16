"""Unit tests for provider-neutral search boundary contracts."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.modules.search.contracts import (
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
        values=(0.25, -0.5, 0.75),
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
            values=(float("nan"),),
            embedding_config_version="embedding-v1",
        )


def test_dense_retrieval_contracts_reject_non_finite_provider_values() -> None:
    with pytest.raises(ValidationError, match="finite"):
        DenseRetrievalQuery(
            query_vector=(float("inf"),),
            filters=RetrievalFilters(),
            embedding_config_version="embedding-v1",
            dense_config_version="dense-v1",
            candidate_limit=10,
        )
    with pytest.raises(ValidationError):
        DenseRetrievalHit(chunk_id=CHUNK_ID, raw_score=float("nan"))


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
