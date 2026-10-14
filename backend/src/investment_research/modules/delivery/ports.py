"""Application-owned outbox persistence within caller-owned transactions."""

from typing import Protocol
from uuid import UUID

from investment_research.modules.delivery.domain import OutboxEvent
from investment_research.modules.identity.domain import WorkspaceContext


class OutboxScopeError(ValueError):
    """Reject an unavailable or incompatible target without exposing identifiers."""


class OutboxRepository(Protocol):
    def add_processing_event(self, context: WorkspaceContext, event: OutboxEvent) -> OutboxEvent:
        """Stage one initial intent idempotently; never commit or publish it here."""

    def get_by_id(self, context: WorkspaceContext, event_id: UUID) -> OutboxEvent | None:
        """Return the same absence for unknown and foreign event identifiers."""

    def cancel_processing_events(
        self, context: WorkspaceContext, processing_run_ids: tuple[UUID, ...]
    ) -> None:
        """Detach content-free delivery history inside the canonical purge transaction."""
