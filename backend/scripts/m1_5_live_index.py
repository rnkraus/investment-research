"""Owned Pinecone namespace lifecycle for one sequential M1.5 evaluation invocation."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from time import monotonic, sleep
from types import TracebackType
from typing import Literal
from uuid import uuid4

from investment_research.infrastructure.search.pinecone import (
    PineconeDenseIndexWriter,
    PineconeDenseRetriever,
)
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.search.application import dense_index_namespace, dense_vector_id
from investment_research.modules.search.contracts import (
    M1_EMBEDDING_BATCH_LIMIT,
    DenseIndexWriteRequest,
    DenseIndexWriteResult,
    DenseRetrievalHit,
    DenseRetrievalQuery,
    DenseVectorId,
)
from scripts.plan_m1_5_live_baseline import MAXIMUM_DOCUMENT_VECTORS

VISIBILITY_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.5
type CleanupStep = Literal[
    "namespace_delete", "namespace_absence", "writer_close", "query_client_close"
]


class LiveIndexLifecycleError(RuntimeError):
    """Report a fixed lifecycle invariant without provider or document content."""


class LiveIndexCleanupError(LiveIndexLifecycleError):
    def __init__(self, steps: tuple[CleanupStep, ...], *, body_failed: bool) -> None:
        super().__init__("M1.5 isolated index cleanup failed: " + ",".join(steps))
        self.steps = steps
        self.body_failed = body_failed


def _new_context() -> WorkspaceContext:
    return WorkspaceContext(user_id=uuid4(), workspace_id=uuid4())


@dataclass(slots=True, repr=False)
class IsolatedEvaluationIndex:
    """Expose existing index ports only for a fresh, internally owned workspace."""

    writer: PineconeDenseIndexWriter
    retriever: PineconeDenseRetriever
    maximum_vectors: int = MAXIMUM_DOCUMENT_VECTORS
    _context: WorkspaceContext = field(default_factory=_new_context, init=False)
    _attempted_ids: dict[DenseVectorId, None] = field(default_factory=dict, init=False)
    _closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if (
            type(self.maximum_vectors) is not int
            or not 0 < self.maximum_vectors <= MAXIMUM_DOCUMENT_VECTORS
        ):
            raise LiveIndexLifecycleError("the evaluation vector limit is invalid")

    @property
    def context(self) -> WorkspaceContext:
        return self._context

    @property
    def attempted_vector_count(self) -> int:
        return len(self._attempted_ids)

    def _require_context(self, context: WorkspaceContext) -> None:
        if self._closed or context != self._context:
            raise LiveIndexLifecycleError("the evaluation index scope is unavailable")

    def upsert(
        self, context: WorkspaceContext, request: DenseIndexWriteRequest
    ) -> DenseIndexWriteResult:
        self._require_context(context)
        ids = tuple(vector.vector_id for vector in request.vectors)
        if any(
            vector.vector_id
            != dense_vector_id(
                self._context,
                chunk_id=vector.chunk_id,
                embedding_config_version=vector.embedding_config_version,
            )
            for vector in request.vectors
        ):
            raise LiveIndexLifecycleError("a vector does not belong to the evaluation workspace")
        if len(set(self._attempted_ids).union(ids)) > self.maximum_vectors:
            raise LiveIndexLifecycleError("the evaluation vector limit is exhausted")
        # A timeout does not prove the write failed. Track IDs before dispatch for cleanup.
        self._attempted_ids.update(dict.fromkeys(ids))
        return self.writer.upsert(self._context, request)

    def fetch_visible(
        self,
        context: WorkspaceContext,
        vector_ids: Sequence[DenseVectorId],
        *,
        timeout_seconds: float | None = None,
    ) -> Sequence[DenseVectorId]:
        self._require_context(context)
        if not set(vector_ids) <= self._attempted_ids.keys():
            raise LiveIndexLifecycleError("visibility IDs are outside the evaluation invocation")
        return self.writer.fetch_visible(self._context, vector_ids, timeout_seconds=timeout_seconds)

    def retrieve(
        self, context: WorkspaceContext, query: DenseRetrievalQuery
    ) -> Sequence[DenseRetrievalHit]:
        self._require_context(context)
        return self.retriever.retrieve(self._context, query)

    def wait_until_visible(self) -> None:
        self._require_context(self._context)
        self._await_state(visible=True, deadline=monotonic() + VISIBILITY_TIMEOUT_SECONDS)

    def _await_state(self, *, visible: bool, deadline: float) -> None:
        ids = tuple(self._attempted_ids)
        if not ids:
            return
        while (remaining := deadline - monotonic()) > 0:
            complete = True
            for start in range(0, len(ids), M1_EMBEDDING_BATCH_LIMIT):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise LiveIndexLifecycleError("the evaluation visibility deadline expired")
                batch = ids[start : start + M1_EMBEDDING_BATCH_LIMIT]
                observed = tuple(
                    self.writer.fetch_visible(
                        self._context,
                        batch,
                        timeout_seconds=remaining,
                    )
                )
                if monotonic() >= deadline:
                    raise LiveIndexLifecycleError("the evaluation visibility deadline expired")
                if len(observed) != len(set(observed)) or not set(observed) <= set(batch):
                    raise LiveIndexLifecycleError("the evaluation visibility response is invalid")
                complete = complete and (set(observed) == set(batch) if visible else not observed)
            if complete:
                return
            remaining = deadline - monotonic()
            if remaining > 0:
                sleep(min(POLL_INTERVAL_SECONDS, remaining))
        raise LiveIndexLifecycleError("the evaluation visibility deadline expired")

    def close(self, *, body_failed: bool = False) -> None:
        """Delete the owned namespace, verify attempted vectors are absent, and close clients."""

        if self._closed:
            return
        steps: list[CleanupStep] = []
        try:
            if self._attempted_ids:
                deadline = monotonic() + VISIBILITY_TIMEOUT_SECONDS
                try:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise LiveIndexLifecycleError("the evaluation cleanup deadline expired")
                    self.writer.index.delete(
                        delete_all=True,
                        namespace=dense_index_namespace(self._context),
                        timeout=min(self.writer.operation_timeout_seconds, remaining),
                    )
                except Exception:
                    steps.append("namespace_delete")
                else:
                    try:
                        self._await_state(visible=False, deadline=deadline)
                    except Exception:
                        steps.append("namespace_absence")
        finally:
            self._closed = True
            closers: tuple[
                tuple[PineconeDenseIndexWriter | PineconeDenseRetriever, CleanupStep], ...
            ] = (
                (self.writer, "writer_close"),
                (self.retriever, "query_client_close"),
            )
            for client, step in closers:
                try:
                    client.close()
                except Exception:
                    steps.append(step)
        if steps:
            raise LiveIndexCleanupError(tuple(steps), body_failed=body_failed) from None

    def __enter__(self) -> IsolatedEvaluationIndex:
        self._require_context(self._context)
        return self

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close(body_failed=error is not None)
