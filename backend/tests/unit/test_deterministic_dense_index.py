"""Unit tests for the shared credential-free SQLite dense-index adapter."""

import sqlite3
from pathlib import Path
from uuid import UUID

import pytest

from investment_research.infrastructure.search.deterministic_index import (
    DeterministicSqliteDenseIndex,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.application import dense_vector_id
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    DenseIndexVector,
    DenseIndexWriteRequest,
    DenseRetrievalQuery,
)
from investment_research.modules.search.domain import RetrievalFilters
from investment_research.modules.search.ports import SearchProviderContractError

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
FOREIGN_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000003")
FIRST_CHUNK_ID = UUID("10000000-0000-4000-8000-000000000001")
SECOND_CHUNK_ID = UUID("10000000-0000-4000-8000-000000000002")
DOCUMENT_VERSION_ID = UUID("20000000-0000-4000-8000-000000000001")
CHUNK_SET_ID = UUID("30000000-0000-4000-8000-000000000001")
EMBEDDING_CONFIG_VERSION = "openai-text-embedding-3-small-1536-v1"
DENSE_INDEX_CONFIG_VERSION = "pinecone-serverless-cosine-1536-aws-us-east-1-v1"


def make_context(workspace_id: UUID = WORKSPACE_ID) -> WorkspaceContext:
    """Build one trusted context for a local acceptance workspace."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=workspace_id)


def basis_vector(index: int) -> tuple[float, ...]:
    """Return one finite nonzero vector with a single selected dimension."""

    return tuple(1.0 if position == index else 0.0 for position in range(M1_EMBEDDING_DIMENSIONS))


def make_vector(
    chunk_id: UUID,
    values: tuple[float, ...],
    *,
    context: WorkspaceContext | None = None,
    embedding_config_version: str = EMBEDDING_CONFIG_VERSION,
    dense_index_config_version: str = DENSE_INDEX_CONFIG_VERSION,
) -> DenseIndexVector:
    """Build one application-valid vector with its deterministic identity."""

    resolved_context = context or make_context()
    return DenseIndexVector(
        vector_id=dense_vector_id(
            resolved_context,
            chunk_id=chunk_id,
            embedding_config_version=embedding_config_version,
        ),
        values=values,
        chunk_id=chunk_id,
        document_version_id=DOCUMENT_VERSION_ID,
        chunk_set_id=CHUNK_SET_ID,
        embedding_config_version=embedding_config_version,
        dense_index_config_version=dense_index_config_version,
    )


def make_request(*vectors: DenseIndexVector) -> DenseIndexWriteRequest:
    """Build one coherent provider-neutral index request."""

    return DenseIndexWriteRequest(
        vectors=vectors,
        dense_index_config_version=DENSE_INDEX_CONFIG_VERSION,
    )


def make_query(values: tuple[float, ...]) -> DenseRetrievalQuery:
    """Build one bounded retrieval query for the frozen M1 configurations."""

    return DenseRetrievalQuery(
        query_vector=values,
        filters=RetrievalFilters(),
        embedding_config_version=EMBEDDING_CONFIG_VERSION,
        dense_index_config_version=DENSE_INDEX_CONFIG_VERSION,
        candidate_limit=20,
    )


def test_writer_and_reader_share_idempotent_safe_state(tmp_path: Path) -> None:
    """Persist only safe vector metadata across separate adapter instances."""

    database_path = tmp_path / "dense-index.sqlite3"
    writer = DeterministicSqliteDenseIndex(database_path)
    first = make_vector(FIRST_CHUNK_ID, basis_vector(0))
    second = make_vector(SECOND_CHUNK_ID, basis_vector(1))

    first_result = writer.upsert(make_context(), make_request(first, second))
    repeated_result = writer.upsert(make_context(), make_request(first, second))
    reader = DeterministicSqliteDenseIndex(database_path)

    assert first_result == repeated_result
    assert reader.fetch_visible(
        make_context(),
        (second.vector_id, first.vector_id),
    ) == (second.vector_id, first.vector_id)

    with sqlite3.connect(database_path) as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(dense_vectors)")}
        row_count = connection.execute("SELECT COUNT(*) FROM dense_vectors").fetchone()
    assert "text" not in columns
    assert "question" not in columns
    assert row_count == (2,)


def test_retrieval_ranks_similarity_and_scopes_workspace_and_versions(tmp_path: Path) -> None:
    """Return only matching workspace/configuration records in stable score order."""

    index = DeterministicSqliteDenseIndex(tmp_path / "dense-index.sqlite3")
    first = make_vector(FIRST_CHUNK_ID, basis_vector(0))
    second = make_vector(SECOND_CHUNK_ID, basis_vector(1))
    index.upsert(make_context(), make_request(first, second))

    hits = index.retrieve(make_context(), make_query(basis_vector(1)))

    assert [hit.chunk_id for hit in hits] == [SECOND_CHUNK_ID, FIRST_CHUNK_ID]
    assert hits[0].raw_score == pytest.approx(1.0)
    assert hits[1].raw_score == pytest.approx(0.0)
    assert (
        index.retrieve(
            make_context(FOREIGN_WORKSPACE_ID),
            make_query(basis_vector(1)),
        )
        == ()
    )
    mismatched_query = make_query(basis_vector(1)).model_copy(
        update={"embedding_config_version": "m1-other-embedding-v1"}
    )
    assert index.retrieve(make_context(), mismatched_query) == ()


def test_upsert_rejects_inconsistent_vector_identity(tmp_path: Path) -> None:
    """Refuse a vector whose identifier does not match its trusted workspace and chunk."""

    index = DeterministicSqliteDenseIndex(tmp_path / "dense-index.sqlite3")
    vector = make_vector(FIRST_CHUNK_ID, basis_vector(0)).model_copy(
        update={"vector_id": "v1:" + "f" * 64}
    )

    with pytest.raises(SearchProviderContractError, match="inconsistent vector identity"):
        index.upsert(make_context(), make_request(vector))
