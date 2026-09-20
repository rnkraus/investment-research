"""Application-owned contracts for optional operational tracing."""

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.domain import (
    ProviderCostSummary,
    ProviderOperation,
)

type TraceAttributeValue = str | bool | int | float
type TraceAttributes = Mapping[str, TraceAttributeValue]
type OperationSpanName = Literal[
    "document.capture",
    "embedding.document",
    "embedding.query",
    "evidence.evaluate_support",
    "evidence.validate",
    "pinecone.query",
    "pinecone.upsert",
    "processing.index",
    "processing.parse",
    "research.generate",
    "research.retrieve",
]

APPROVED_OPERATION_SPAN_NAMES: frozenset[OperationSpanName] = frozenset(
    {
        "document.capture",
        "embedding.document",
        "embedding.query",
        "evidence.evaluate_support",
        "evidence.validate",
        "pinecone.query",
        "pinecone.upsert",
        "processing.index",
        "processing.parse",
        "research.generate",
        "research.retrieve",
    }
)
APPROVED_TRACE_ATTRIBUTE_NAMES = frozenset(
    {
        "answer.id",
        "candidate.count",
        "chunk.count",
        "configuration.version",
        "correlation.id",
        "cost.micro_usd",
        "duration.ms",
        "error.type",
        "input.units",
        "job.id",
        "lifecycle.state",
        "model.name",
        "operation.kind",
        "outcome",
        "output.units",
        "processing.run.id",
        "provider.name",
        "request.id",
        "research.request.id",
        "workspace.id",
    }
)


class OperationSpan(Protocol):
    """Record bounded metadata for one application operation."""

    @property
    def trace_id(self) -> str | None:
        """Return the active W3C trace ID, or none when tracing is unavailable."""

    def set_attribute(self, name: str, value: TraceAttributeValue) -> None:
        """Attach one safe scalar attribute without raw document or prompt content."""

    def record_error(self, error_type: str) -> None:
        """Record one safe error category without exception text or traceback data."""


class OperationTracer(Protocol):
    """Open optional spans without coupling application code to a tracing SDK."""

    def start_span(
        self,
        name: str,
        *,
        attributes: TraceAttributes | None = None,
    ) -> AbstractContextManager[OperationSpan]:
        """Return a context manager for one named application operation."""


class ProviderOperationRepository(Protocol):
    """Persist and read content-free provider accounting inside one workspace."""

    def acquire_budget_lock(self, context: WorkspaceContext) -> None:
        """Serialize one workspace's budget check until the transaction ends."""

    def add(self, context: WorkspaceContext, operation: ProviderOperation) -> None:
        """Stage one STARTED operation without committing the caller's transaction."""

    def get_by_id(
        self,
        context: WorkspaceContext,
        operation_id: UUID,
    ) -> ProviderOperation | None:
        """Return an operation only from the trusted workspace."""

    def finalize(self, context: WorkspaceContext, operation: ProviderOperation) -> bool:
        """Stage one valid terminal transition and report whether the row exists."""

    def list_by_correlation_id(
        self,
        context: WorkspaceContext,
        correlation_id: UUID,
    ) -> Sequence[ProviderOperation]:
        """Return the workspace's operations for one safe journey identifier."""

    def summarize_cost(
        self,
        context: WorkspaceContext,
        *,
        started_at_from: datetime,
        started_at_before: datetime,
    ) -> ProviderCostSummary:
        """Return actual terminal cost plus active reservations for a half-open window."""


class ProviderOperationUnitOfWork(Protocol):
    """Provider-operation repository participating in one short transaction."""

    @property
    def provider_operations(self) -> ProviderOperationRepository:
        """Return the repository used for one accounting state change."""


type ProviderOperationUnitOfWorkFactory = Callable[
    [],
    AbstractContextManager[ProviderOperationUnitOfWork],
]
