"""Shared SQLite dense-index adapter for credential-free runtime acceptance."""

import sqlite3
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from math import sqrt
from pathlib import Path
from uuid import UUID

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.application import (
    DENSE_VECTOR_ID_ADAPTER,
    dense_index_namespace,
    dense_vector_id,
)
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_BATCH_LIMIT,
    M1_EMBEDDING_DIMENSIONS,
    DenseIndexWriteRequest,
    DenseIndexWriteResult,
    DenseRetrievalHit,
    DenseRetrievalQuery,
    DenseVectorId,
)
from investment_research.modules.search.ports import (
    SearchProviderConfigurationError,
    SearchProviderContractError,
    SearchProviderTransientError,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dense_vectors (
    namespace TEXT NOT NULL,
    vector_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    document_version_id TEXT NOT NULL,
    chunk_set_id TEXT NOT NULL,
    embedding_config_version TEXT NOT NULL,
    dense_index_config_version TEXT NOT NULL,
    vector_values BLOB NOT NULL,
    PRIMARY KEY (namespace, vector_id)
);
CREATE INDEX IF NOT EXISTS ix_dense_vectors_lookup
ON dense_vectors (namespace, embedding_config_version, dense_index_config_version);
"""


@dataclass(frozen=True, slots=True)
class DeterministicSqliteDenseIndex:
    """Persist safe vector metadata across isolated API and worker test processes."""

    database_path: Path

    def __post_init__(self) -> None:
        """Create the private test schema without accepting an implicit parent path."""

        if not self.database_path.parent.is_dir():
            raise SearchProviderConfigurationError(
                "deterministic dense-index directory is unavailable"
            )
        try:
            with self._connect() as connection:
                connection.executescript(_SCHEMA)
        except sqlite3.DatabaseError:
            raise SearchProviderConfigurationError(
                "deterministic dense index could not be initialized"
            ) from None

    def upsert(
        self,
        context: WorkspaceContext,
        request: DenseIndexWriteRequest,
    ) -> DenseIndexWriteResult:
        """Idempotently store validated vectors without source text or display metadata."""

        namespace = dense_index_namespace(context)
        rows: list[tuple[str, str, str, str, str, str, str, bytes]] = []
        for vector in request.vectors:
            expected_id = dense_vector_id(
                context,
                chunk_id=vector.chunk_id,
                embedding_config_version=vector.embedding_config_version,
            )
            if vector.vector_id != expected_id:
                raise SearchProviderContractError(
                    "deterministic dense index received an inconsistent vector identity"
                )
            rows.append(
                (
                    namespace,
                    vector.vector_id,
                    str(vector.chunk_id),
                    str(vector.document_version_id),
                    str(vector.chunk_set_id),
                    vector.embedding_config_version,
                    vector.dense_index_config_version,
                    self._encode_vector(vector.values),
                )
            )

        try:
            with self._connect() as connection:
                connection.executemany(
                    """
                    INSERT INTO dense_vectors (
                        namespace,
                        vector_id,
                        chunk_id,
                        document_version_id,
                        chunk_set_id,
                        embedding_config_version,
                        dense_index_config_version,
                        vector_values
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (namespace, vector_id) DO UPDATE SET
                        chunk_id = excluded.chunk_id,
                        document_version_id = excluded.document_version_id,
                        chunk_set_id = excluded.chunk_set_id,
                        embedding_config_version = excluded.embedding_config_version,
                        dense_index_config_version = excluded.dense_index_config_version,
                        vector_values = excluded.vector_values
                    """,
                    rows,
                )
        except sqlite3.OperationalError:
            raise SearchProviderTransientError(
                "deterministic dense index is temporarily unavailable"
            ) from None
        except sqlite3.DatabaseError:
            raise SearchProviderConfigurationError(
                "deterministic dense index rejected the write"
            ) from None

        return DenseIndexWriteResult(
            vector_ids=tuple(vector.vector_id for vector in request.vectors)
        )

    def fetch_visible(
        self,
        context: WorkspaceContext,
        vector_ids: Sequence[DenseVectorId],
    ) -> Sequence[DenseVectorId]:
        """Return requested visible identities in request order and workspace scope."""

        requested_ids = tuple(
            DENSE_VECTOR_ID_ADAPTER.validate_python(vector_id) for vector_id in vector_ids
        )
        if not requested_ids:
            raise ValueError("dense index visibility check requires vector IDs")
        if len(requested_ids) > M1_EMBEDDING_BATCH_LIMIT:
            raise ValueError("dense index visibility check exceeds the batch limit")
        if len(requested_ids) != len(set(requested_ids)):
            raise ValueError("dense index visibility check requires unique vector IDs")

        placeholders = ", ".join("?" for _ in requested_ids)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT vector_id
                    FROM dense_vectors
                    WHERE namespace = ? AND vector_id IN ({placeholders})
                    """,
                    (dense_index_namespace(context), *requested_ids),
                ).fetchall()
        except sqlite3.DatabaseError:
            raise SearchProviderTransientError(
                "deterministic dense index is temporarily unavailable"
            ) from None

        visible_ids = {str(row[0]) for row in rows}
        return tuple(vector_id for vector_id in requested_ids if vector_id in visible_ids)

    def retrieve(
        self,
        context: WorkspaceContext,
        query: DenseRetrievalQuery,
    ) -> Sequence[DenseRetrievalHit]:
        """Rank safe workspace-scoped vector records by deterministic cosine similarity."""

        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT vector_id, chunk_id, vector_values
                    FROM dense_vectors
                    WHERE namespace = ?
                      AND embedding_config_version = ?
                      AND dense_index_config_version = ?
                    """,
                    (
                        dense_index_namespace(context),
                        query.embedding_config_version,
                        query.dense_index_config_version,
                    ),
                ).fetchall()
        except sqlite3.DatabaseError:
            raise SearchProviderTransientError(
                "deterministic dense index is temporarily unavailable"
            ) from None

        ranked: list[tuple[float, UUID]] = []
        for vector_id_value, chunk_id_value, encoded_values in rows:
            try:
                chunk_id = UUID(str(chunk_id_value))
                vector_id = DENSE_VECTOR_ID_ADAPTER.validate_python(str(vector_id_value))
                values = self._decode_vector(bytes(encoded_values))
            except TypeError, ValueError:
                raise SearchProviderContractError(
                    "deterministic dense index contains malformed vector metadata"
                ) from None
            if vector_id != dense_vector_id(
                context,
                chunk_id=chunk_id,
                embedding_config_version=query.embedding_config_version,
            ):
                raise SearchProviderContractError(
                    "deterministic dense index contains an inconsistent vector identity"
                )
            ranked.append((self._cosine_similarity(query.query_vector, values), chunk_id))

        ranked.sort(key=lambda item: (-item[0], item[1].hex))
        return tuple(
            DenseRetrievalHit(chunk_id=chunk_id, raw_score=score)
            for score, chunk_id in ranked[: query.candidate_limit]
        )

    def close(self) -> None:
        """Match provider adapter lifecycle without retaining a process-local connection."""

    def _connect(self) -> sqlite3.Connection:
        """Open one bounded connection so processes never share Python connection state."""

        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @staticmethod
    def _encode_vector(values: tuple[float, ...]) -> bytes:
        """Store fixed-width network-order doubles without unsafe object serialization."""

        return struct.pack(f"!{M1_EMBEDDING_DIMENSIONS}d", *values)

    @staticmethod
    def _decode_vector(encoded: bytes) -> tuple[float, ...]:
        """Reject truncated or expanded records before similarity calculation."""

        expected_size = M1_EMBEDDING_DIMENSIONS * 8
        if len(encoded) != expected_size:
            raise ValueError("invalid deterministic vector byte length")
        return struct.unpack(f"!{M1_EMBEDDING_DIMENSIONS}d", encoded)

    @staticmethod
    def _cosine_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
        """Calculate bounded similarity while rejecting unusable zero vectors."""

        left_norm = sqrt(sum(value * value for value in left))
        right_norm = sqrt(sum(value * value for value in right))
        if left_norm == 0 or right_norm == 0:
            raise SearchProviderContractError(
                "deterministic dense index cannot compare a zero vector"
            )
        return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)
