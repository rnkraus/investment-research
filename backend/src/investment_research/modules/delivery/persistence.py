"""Module-owned SQLAlchemy mapping for content-free outbox bookkeeping."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from investment_research.infrastructure.database.base import Base
from investment_research.modules.delivery.domain import (
    DeliveryErrorCategory,
    OutboxEvent,
    OutboxKind,
    OutboxState,
)

OUTBOX_CHECKS = (
    ("schema_version = 'outbox-event.v1'", "schema_version"),
    ("kind = 'PROCESS_DOCUMENT'", "supported_kind"),
    ("generation BETWEEN 0 AND 2", "bounded_generation"),
    ("attempt_count BETWEEN 0 AND 3", "bounded_attempt_count"),
    (
        "state IN ('PENDING', 'CLAIMED', 'RETRY_WAIT', 'PUBLISHED', 'BLOCKED', 'CANCELLED')",
        "delivery_state",
    ),
    (
        "last_error_category IS NULL OR last_error_category IN "
        "('DEPENDENCY_UNAVAILABLE', 'INVALID_JOB', 'SOURCE_INELIGIBLE', 'ATTEMPTS_EXHAUSTED')",
        "safe_error_category",
    ),
    (
        "(state = 'CANCELLED' AND processing_run_id IS NULL) OR "
        "(processing_run_id IS NOT NULL AND processing_run_id = target_id)",
        "scoped_target_binding",
    ),
    (
        "(next_attempt_at IS NULL OR next_attempt_at >= created_at) AND "
        "(published_at IS NULL OR published_at >= created_at)",
        "ordered_times",
    ),
    (
        "(state = 'CLAIMED' AND attempt_count > 0 AND lease_id IS NOT NULL AND "
        "lease_expires_at > created_at AND lease_expires_at IS NOT NULL) OR "
        "(state <> 'CLAIMED' AND lease_id IS NULL AND lease_expires_at IS NULL)",
        "consistent_lease",
    ),
    (
        "(state IN ('PENDING', 'RETRY_WAIT') AND next_attempt_at IS NOT NULL) OR "
        "(state NOT IN ('PENDING', 'RETRY_WAIT') AND next_attempt_at IS NULL)",
        "consistent_due_time",
    ),
    (
        "state <> 'PENDING' OR (attempt_count = 0 AND last_error_category IS NULL)",
        "initial_pending_state",
    ),
    (
        "state <> 'RETRY_WAIT' OR (attempt_count BETWEEN 1 AND 2 AND "
        "last_error_category IS NOT NULL AND last_error_category = 'DEPENDENCY_UNAVAILABLE')",
        "bounded_retry_wait",
    ),
    (
        "(state = 'PUBLISHED' AND published_at IS NOT NULL AND attempt_count > 0) OR "
        "state = 'CANCELLED' OR (state <> 'PUBLISHED' AND published_at IS NULL)",
        "consistent_publication",
    ),
    ("state <> 'BLOCKED' OR last_error_category IS NOT NULL", "blocked_reason"),
)


class OutboxEventRow(Base):
    """Delivery bookkeeping whose detached IDs outlive a permanently removed source."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("workspace_id", "event_id"),
        UniqueConstraint("workspace_id", "kind", "target_id", "generation"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "processing_run_id"],
            ["processing_runs.workspace_id", "processing_runs.id"],
            ondelete="RESTRICT",
        ),
        *(CheckConstraint(expression, name=name) for expression, name in OUTBOX_CHECKS),
        Index("ix_outbox_events_due", "state", "next_attempt_at", "event_id"),
        Index("ix_outbox_events_processing_target", "workspace_id", "processing_run_id"),
    )

    event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[OutboxKind] = mapped_column(
        SqlEnum(OutboxKind, native_enum=False, create_constraint=False, length=16), nullable=False
    )
    target_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    processing_run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[OutboxState] = mapped_column(
        SqlEnum(OutboxState, native_enum=False, create_constraint=False, length=16), nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_category: Mapped[DeliveryErrorCategory | None] = mapped_column(
        SqlEnum(DeliveryErrorCategory, native_enum=False, create_constraint=False, length=32)
    )

    def to_domain(self) -> OutboxEvent:
        return OutboxEvent.model_validate(
            {field: getattr(self, field) for field in OutboxEvent.model_fields}
        )
