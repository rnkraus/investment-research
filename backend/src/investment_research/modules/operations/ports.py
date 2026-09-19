"""Application-owned contracts for optional operational tracing."""

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.domain import (
    ProviderCostSummary,
    ProviderOperation,
)

type TraceAttributeValue = str | bool | int | float
type TraceAttributes = Mapping[str, TraceAttributeValue]


class OperationSpan(Protocol):
    """Record bounded metadata for one application operation."""

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
