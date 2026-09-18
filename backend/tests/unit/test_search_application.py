"""Unit tests for lexical-search query construction and contracts."""

from datetime import UTC, datetime, timedelta, timezone
from math import inf
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.application import (
    DenseIndexRelationshipError,
    EmbeddingResponseContractError,
    build_dense_index_vector,
    build_lexical_search_query,
    dense_index_namespace,
    dense_vector_id,
    normalize_lexical_query,
    validate_embedding_response,
)
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    EmbeddingInput,
    EmbeddingPurpose,
    EmbeddingRequest,
    EmbeddingVector,
    LexicalSearchHit,
)
from investment_research.modules.search.domain import RetrievalFilters

DOCUMENT_ID = UUID("10000000-0000-4000-8000-000000000001")
VERSION_ID = UUID("10000000-0000-4000-8000-000000000002")
BLOCK_ID = UUID("10000000-0000-4000-8000-000000000003")
CHUNK_ID = UUID("10000000-0000-4000-8000-000000000004")
SECOND_CHUNK_ID = UUID("10000000-0000-4000-8000-000000000005")
CHUNK_SET_ID = UUID("10000000-0000-4000-8000-000000000006")
WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000010")
FOREIGN_WORKSPACE_ID = UUID("10000000-0000-4000-8000-000000000011")
USER_ID = UUID("10000000-0000-4000-8000-000000000012")
CUTOFF_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
VECTOR_VALUES = (0.0,) * M1_EMBEDDING_DIMENSIONS
EXPECTED_VECTOR_ID = "v1:744949988e775492e6767a69a22cb197dc7c8f40736c3ecf075dd4aa7e6ca899"


def make_hit(**changes: object) -> LexicalSearchHit:
    """Build one valid lexical hit with optional field changes."""

    values = {
        "chunk_id": CHUNK_ID,
        "document_id": DOCUMENT_ID,
        "document_version_id": VERSION_ID,
        "structure_block_id": BLOCK_ID,
        "page_number": 2,
        "text": "FY2025 revenue was $184.2 million.",
        "raw_score": 0.75,
        **changes,
    }
    return LexicalSearchHit.model_validate(values)


def test_normalize_lexical_query_canonicalizes_unicode_case_and_whitespace() -> None:
    query = "  \uff26\uff39\uff12\uff10\uff12\uff15\tREVENUE\nGrowth  "

    assert normalize_lexical_query(query) == "fy2025 revenue growth"


@pytest.mark.parametrize(
    "query",
    [" \n\t ", "x" * 2_001],
    ids=["no-visible-text", "over-character-limit"],
)
def test_normalize_lexical_query_rejects_invalid_input(query: str) -> None:
    with pytest.raises(ValidationError):
        normalize_lexical_query(query)


def test_build_lexical_search_query_preserves_effective_filters() -> None:
    filters = RetrievalFilters(
        document_ids=(DOCUMENT_ID,),
        available_at_cutoff=CUTOFF_AT,
        captured_at_cutoff=CUTOFF_AT,
    )

    query = build_lexical_search_query(
        query_text="  What was FY2025 REVENUE?  ",
        filters=filters,
        lexical_config_version="m0.0-english-lexical-v1",
        candidate_limit=10,
    )

    assert query.normalized_query == "what was fy2025 revenue?"
    assert query.filters is filters
    assert query.lexical_config_version == "m0.0-english-lexical-v1"
    assert query.candidate_limit == 10


@pytest.mark.parametrize("candidate_limit", [0, 101])
def test_build_lexical_search_query_rejects_unbounded_candidate_limits(
    candidate_limit: int,
) -> None:
    with pytest.raises(ValidationError):
        build_lexical_search_query(
            query_text="What was FY2025 revenue?",
            filters=RetrievalFilters(),
            lexical_config_version="m0.0-english-lexical-v1",
            candidate_limit=candidate_limit,
        )


def test_embedding_response_requires_exact_order_and_configuration() -> None:
    request = EmbeddingRequest(
        inputs=(
            EmbeddingInput(
                input_id=CHUNK_ID,
                text="Canonical chunk one",
                purpose=EmbeddingPurpose.DOCUMENT,
            ),
            EmbeddingInput(
                input_id=SECOND_CHUNK_ID,
                text="Canonical chunk two",
                purpose=EmbeddingPurpose.DOCUMENT,
            ),
        ),
        embedding_config_version="embedding-v1",
    )
    first = EmbeddingVector(
        input_id=CHUNK_ID,
        values=VECTOR_VALUES,
        embedding_config_version="embedding-v1",
    )
    second = EmbeddingVector(
        input_id=SECOND_CHUNK_ID,
        values=VECTOR_VALUES,
        embedding_config_version="embedding-v1",
    )

    assert validate_embedding_response(request, (first, second)) == (first, second)

    with pytest.raises(EmbeddingResponseContractError, match="exactly once and in order"):
        validate_embedding_response(request, (second, first))
    with pytest.raises(EmbeddingResponseContractError, match="exactly once and in order"):
        validate_embedding_response(request, (first,))
    with pytest.raises(EmbeddingResponseContractError, match="configuration version"):
        validate_embedding_response(
            request,
            (first, second.model_copy(update={"embedding_config_version": "embedding-v2"})),
        )


def test_dense_identity_and_metadata_are_workspace_scoped_and_deterministic() -> None:
    context = WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)
    foreign_context = WorkspaceContext(user_id=USER_ID, workspace_id=FOREIGN_WORKSPACE_ID)
    embedding = EmbeddingVector(
        input_id=CHUNK_ID,
        values=VECTOR_VALUES,
        embedding_config_version="embedding-v1",
    )

    assert dense_index_namespace(context) == f"ws-{WORKSPACE_ID.hex}"
    assert (
        dense_vector_id(
            context,
            chunk_id=CHUNK_ID,
            embedding_config_version="embedding-v1",
        )
        == EXPECTED_VECTOR_ID
    )
    assert dense_vector_id(
        foreign_context,
        chunk_id=CHUNK_ID,
        embedding_config_version="embedding-v1",
    ) != dense_vector_id(
        context,
        chunk_id=CHUNK_ID,
        embedding_config_version="embedding-v1",
    )

    vector = build_dense_index_vector(
        context,
        chunk_id=CHUNK_ID,
        document_version_id=VERSION_ID,
        chunk_set_id=CHUNK_SET_ID,
        embedding=embedding,
        dense_index_config_version="dense-v1",
    )

    assert vector.vector_id == EXPECTED_VECTOR_ID
    assert vector.chunk_id == CHUNK_ID
    assert vector.document_version_id == VERSION_ID
    assert vector.chunk_set_id == CHUNK_SET_ID
    assert vector.embedding_config_version == "embedding-v1"
    assert vector.dense_index_config_version == "dense-v1"

    with pytest.raises(DenseIndexRelationshipError, match="canonical chunk"):
        build_dense_index_vector(
            context,
            chunk_id=SECOND_CHUNK_ID,
            document_version_id=VERSION_ID,
            chunk_set_id=CHUNK_SET_ID,
            embedding=embedding,
            dense_index_config_version="dense-v1",
        )


def test_archive_cutoff_requires_public_availability_cutoff() -> None:
    with pytest.raises(ValidationError, match="requires available_at_cutoff"):
        RetrievalFilters(captured_at_cutoff=CUTOFF_AT)


def test_temporal_filters_normalize_to_utc() -> None:
    offset_cutoff = CUTOFF_AT.astimezone(timezone(timedelta(hours=2)))

    filters = RetrievalFilters(
        available_at_cutoff=offset_cutoff,
        captured_at_cutoff=offset_cutoff,
    )

    assert filters.available_at_cutoff == CUTOFF_AT
    assert filters.captured_at_cutoff == CUTOFF_AT


@pytest.mark.parametrize(
    "changes",
    [
        {"text": " \n\t "},
        {"page_number": 0},
        {"raw_score": 0},
        {"raw_score": inf},
    ],
    ids=["no-visible-text", "invalid-page", "zero-score", "infinite-score"],
)
def test_lexical_search_hit_rejects_invalid_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_hit(**changes)
