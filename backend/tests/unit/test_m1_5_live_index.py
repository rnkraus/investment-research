"""Isolated evaluation cleanup and deadline tests using no Pinecone connection."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast
from uuid import uuid4

import pytest

from investment_research.infrastructure.search.pinecone import (
    PineconeDenseIndexWriter,
    PineconeDenseRetriever,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.application import (
    build_dense_index_vector,
    dense_index_namespace,
)
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_DIMENSIONS,
    DenseIndexWriteRequest,
    DenseIndexWriteResult,
    DenseRetrievalHit,
    DenseRetrievalQuery,
    DenseVectorId,
    EmbeddingVector,
)
from investment_research.modules.search.domain import RetrievalFilters
from scripts import m1_5_live_index as live

EMBEDDING_CONFIG = "openai-text-embedding-3-small-1536-v1"
INDEX_CONFIG = "pinecone-serverless-cosine-1536-aws-us-east-1-v1"
MARKER = "synthetic-private-provider-error"


@dataclass
class Clock:
    now: float = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class RecordingWriter:
    clock: Clock
    operation_timeout_seconds: float = 20.0
    fail_upsert: bool = False
    fail_delete: bool = False
    fail_fetch: bool = False
    fail_close: bool = False
    keep_after_delete: bool = False
    add_foreign_response: bool = False
    fetch_delay: float = 0.0
    ready: bool = True
    deleted: bool = False
    closed: bool = False
    writes: list[tuple[WorkspaceContext, DenseIndexWriteRequest]] = field(default_factory=list)
    fetches: list[tuple[WorkspaceContext, tuple[str, ...], float | None]] = field(
        default_factory=list
    )
    deletes: list[tuple[str, bool, float]] = field(default_factory=list)

    @property
    def index(self) -> RecordingWriter:
        return self

    def upsert(
        self, context: WorkspaceContext, request: DenseIndexWriteRequest
    ) -> DenseIndexWriteResult:
        self.writes.append((context, request))
        if self.fail_upsert:
            raise TimeoutError(MARKER)
        return DenseIndexWriteResult(
            vector_ids=tuple(vector.vector_id for vector in request.vectors)
        )

    def fetch_visible(
        self,
        context: WorkspaceContext,
        ids: Sequence[DenseVectorId],
        *,
        timeout_seconds: float | None = None,
    ) -> Sequence[DenseVectorId]:
        self.fetches.append((context, tuple(ids), timeout_seconds))
        self.clock.now += self.fetch_delay
        if self.fail_fetch:
            raise RuntimeError(MARKER)
        if self.add_foreign_response:
            return ("v1:" + "f" * 64,)
        if not self.ready or (self.deleted and not self.keep_after_delete):
            return ()
        return tuple(ids)

    def delete(self, *, namespace: str, delete_all: bool, timeout: float) -> None:
        self.deletes.append((namespace, delete_all, timeout))
        if self.fail_delete:
            raise RuntimeError(MARKER)
        self.deleted = True

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError(MARKER)


@dataclass
class RecordingRetriever:
    calls: list[WorkspaceContext] = field(default_factory=list)
    fail_close: bool = False
    closed: bool = False

    def retrieve(
        self, context: WorkspaceContext, query: DenseRetrievalQuery
    ) -> Sequence[DenseRetrievalHit]:
        self.calls.append(context)
        return ()

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError(MARKER)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    result = Clock()
    monkeypatch.setattr(live, "monotonic", result.monotonic)
    monkeypatch.setattr(live, "sleep", result.sleep)
    return result


def session(
    clock: Clock, *, maximum_vectors: int = 64
) -> tuple[
    live.IsolatedEvaluationIndex,
    RecordingWriter,
    RecordingRetriever,
]:
    writer, retriever = RecordingWriter(clock), RecordingRetriever()
    return (
        live.IsolatedEvaluationIndex(
            cast(PineconeDenseIndexWriter, writer),
            cast(PineconeDenseRetriever, retriever),
            maximum_vectors=maximum_vectors,
        ),
        writer,
        retriever,
    )


def request(context: WorkspaceContext, *, count: int = 1) -> DenseIndexWriteRequest:
    return DenseIndexWriteRequest(
        vectors=tuple(
            build_dense_index_vector(
                context,
                chunk_id=(chunk_id := uuid4()),
                document_version_id=uuid4(),
                chunk_set_id=uuid4(),
                embedding=EmbeddingVector(
                    input_id=chunk_id,
                    values=(0.1,) * M1_EMBEDDING_DIMENSIONS,
                    embedding_config_version=EMBEDDING_CONFIG,
                ),
                dense_index_config_version=INDEX_CONFIG,
            )
            for _ in range(count)
        ),
        dense_index_config_version=INDEX_CONFIG,
    )


def query() -> DenseRetrievalQuery:
    return DenseRetrievalQuery(
        query_vector=(0.1,) * M1_EMBEDDING_DIMENSIONS,
        filters=RetrievalFilters(),
        embedding_config_version=EMBEDDING_CONFIG,
        dense_index_config_version=INDEX_CONFIG,
        candidate_limit=20,
    )


def test_owned_context_is_fresh_and_cleanup_is_scoped_and_idempotent(clock: Clock) -> None:
    index, writer, retriever = session(clock)
    other, _, _ = session(clock)
    assert index.context.workspace_id != other.context.workspace_id
    assert index.context.user_id != other.context.user_id
    with index:
        written = index.upsert(index.context, request(index.context))
        index.wait_until_visible()
        assert index.fetch_visible(index.context, written.vector_ids) == written.vector_ids
        assert not index.retrieve(index.context, query())
    assert writer.deletes == [(dense_index_namespace(index.context), True, 20.0)]
    assert all(context == index.context for context, _, _ in writer.fetches)
    assert writer.closed and retriever.closed
    index.close()
    assert len(writer.deletes) == 1
    with pytest.raises(live.LiveIndexLifecycleError, match="unavailable"):
        index.retrieve(index.context, query())


def test_foreign_context_and_vector_are_denied_before_provider_access(clock: Clock) -> None:
    index, writer, retriever = session(clock)
    foreign, _, _ = session(clock)
    with index:
        with pytest.raises(live.LiveIndexLifecycleError, match="scope"):
            index.upsert(foreign.context, request(foreign.context))
        with pytest.raises(live.LiveIndexLifecycleError, match="workspace"):
            index.upsert(index.context, request(foreign.context))
        with pytest.raises(live.LiveIndexLifecycleError, match="scope"):
            index.retrieve(foreign.context, query())
        with pytest.raises(live.LiveIndexLifecycleError, match="invocation"):
            index.fetch_visible(index.context, ("v1:" + "a" * 64,))
    assert not writer.writes and not writer.fetches and not writer.deletes and not retriever.calls


def test_attempted_write_is_cleaned_even_when_no_receipt_arrives(clock: Clock) -> None:
    index, writer, retriever = session(clock)
    writer.fail_upsert = True
    with pytest.raises(TimeoutError), index:
        index.upsert(index.context, request(index.context))
    assert index.attempted_vector_count == 1
    assert len(writer.deletes) == 1
    assert writer.closed and retriever.closed


def test_unique_vector_limit_counts_before_write_but_allows_idempotent_repeat(clock: Clock) -> None:
    index, writer, _ = session(clock, maximum_vectors=1)
    one = request(index.context)
    with index:
        index.upsert(index.context, one)
        index.upsert(index.context, one)
        with pytest.raises(live.LiveIndexLifecycleError, match="vector limit"):
            index.upsert(index.context, request(index.context))
    assert len(writer.writes) == 2
    assert index.attempted_vector_count == 1


@pytest.mark.parametrize("limit", [0, -1, 65, True])
def test_vector_limit_cannot_be_expanded(clock: Clock, limit: int) -> None:
    with pytest.raises(live.LiveIndexLifecycleError, match="vector limit"):
        session(clock, maximum_vectors=limit)


def test_visibility_batches_share_one_deadline_and_late_success_is_rejected(clock: Clock) -> None:
    index, writer, _ = session(clock)
    with index:
        index.upsert(index.context, request(index.context, count=16))
        index.upsert(index.context, request(index.context))
        writer.fetch_delay = 20.0
        with pytest.raises(live.LiveIndexLifecycleError, match="deadline"):
            index.wait_until_visible()
        assert [timeout for _, _, timeout in writer.fetches] == [30.0, 10.0]
        writer.fetch_delay = 0.0


def test_never_visible_vectors_have_a_bounded_timeout(clock: Clock) -> None:
    index, writer, _ = session(clock)
    writer.ready = False
    with index:
        index.upsert(index.context, request(index.context))
        with pytest.raises(live.LiveIndexLifecycleError, match="deadline"):
            index.wait_until_visible()
        assert clock.now == 30.0


@pytest.mark.parametrize(
    "failure,step",
    [
        ("fail_delete", "namespace_delete"),
        ("fail_fetch", "namespace_absence"),
        ("keep_after_delete", "namespace_absence"),
        ("add_foreign_response", "namespace_absence"),
    ],
)
def test_unverified_cleanup_fails_safely_and_still_closes_both_clients(
    clock: Clock,
    failure: str,
    step: str,
) -> None:
    index, writer, retriever = session(clock)
    setattr(writer, failure, True)
    with pytest.raises(live.LiveIndexCleanupError) as caught, index:
        index.upsert(index.context, request(index.context))
        raise RuntimeError(MARKER)
    assert caught.value.steps == (step,)
    assert caught.value.body_failed is True
    assert MARKER not in str(caught.value)
    assert caught.value.__suppress_context__ is True
    assert writer.closed and retriever.closed


def test_all_client_close_failures_are_reported_without_hiding_the_second(clock: Clock) -> None:
    index, writer, retriever = session(clock)
    writer.fail_close = retriever.fail_close = True
    with pytest.raises(live.LiveIndexCleanupError) as caught, index:
        pass
    assert caught.value.steps == ("writer_close", "query_client_close")
    assert caught.value.body_failed is False
    assert writer.closed and retriever.closed
    assert not writer.deletes


def test_keyboard_interrupt_still_cleans_the_attempted_namespace(clock: Clock) -> None:
    index, writer, retriever = session(clock)
    with pytest.raises(KeyboardInterrupt), index:
        index.upsert(index.context, request(index.context))
        raise KeyboardInterrupt
    assert writer.deleted and writer.closed and retriever.closed
