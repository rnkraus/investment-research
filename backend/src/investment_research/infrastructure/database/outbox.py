"""Cross-module canonical qualification for application-owned outbox persistence."""

from uuid import UUID

from sqlalchemy import and_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from investment_research.modules.delivery.domain import (
    DeliveryErrorCategory,
    OutboxEvent,
    OutboxState,
)
from investment_research.modules.delivery.persistence import OutboxEventRow
from investment_research.modules.delivery.ports import OutboxScopeError
from investment_research.modules.documents.domain import DocumentLifecycleState
from investment_research.modules.documents.persistence import DocumentRow, DocumentVersionRow
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.persistence import ProcessingRunRow


class SqlAlchemyOutboxRepository:
    """Join the caller's transaction without independently committing or publishing."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, context: WorkspaceContext, event_id: UUID) -> OutboxEvent | None:
        row = self._session.scalar(
            select(OutboxEventRow).where(
                OutboxEventRow.workspace_id == context.workspace_id,
                OutboxEventRow.event_id == event_id,
            )
        )
        return None if row is None else row.to_domain()

    def add_processing_event(self, context: WorkspaceContext, event: OutboxEvent) -> OutboxEvent:
        event = OutboxEvent.model_validate(event.model_dump())
        if event.workspace_id != context.workspace_id or event.state is not OutboxState.PENDING:
            raise OutboxScopeError("processing delivery intent is unavailable")
        self._session.flush()
        eligible = self._session.scalar(
            select(ProcessingRunRow.id)
            .join(
                DocumentVersionRow,
                and_(
                    DocumentVersionRow.workspace_id == ProcessingRunRow.workspace_id,
                    DocumentVersionRow.id == ProcessingRunRow.document_version_id,
                ),
            )
            .join(
                DocumentRow,
                and_(
                    DocumentRow.workspace_id == DocumentVersionRow.workspace_id,
                    DocumentRow.id == DocumentVersionRow.document_id,
                ),
            )
            .where(
                ProcessingRunRow.workspace_id == context.workspace_id,
                ProcessingRunRow.id == event.target_id,
                DocumentRow.lifecycle_state == DocumentLifecycleState.ACTIVE,
            )
            .with_for_update(of=DocumentRow)
        )
        if eligible is None:
            raise OutboxScopeError("processing delivery intent is unavailable")
        values = event.model_dump() | {"processing_run_id": event.target_id}
        row = self._session.scalar(
            insert(OutboxEventRow)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["workspace_id", "kind", "target_id", "generation"]
            )
            .returning(OutboxEventRow)
        )
        if row is not None:
            return row.to_domain()
        existing = self.get_by_id(context, event.event_id)
        if existing is None:
            raise OutboxScopeError("processing delivery intent is unavailable")
        return existing

    def cancel_processing_events(
        self, context: WorkspaceContext, processing_run_ids: tuple[UUID, ...]
    ) -> None:
        self._session.execute(
            update(OutboxEventRow)
            .where(
                OutboxEventRow.workspace_id == context.workspace_id,
                OutboxEventRow.processing_run_id.in_(processing_run_ids),
            )
            .values(
                state=OutboxState.CANCELLED,
                processing_run_id=None,
                next_attempt_at=None,
                lease_id=None,
                lease_expires_at=None,
                last_error_category=DeliveryErrorCategory.SOURCE_INELIGIBLE,
            )
        )
