"""SQLAlchemy mapping and scoped repository for canonical provider operations."""

from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import blake2b
from typing import Self
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    case,
    func,
    select,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, Session, mapped_column

from investment_research.infrastructure.database.base import Base
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.operations.domain import (
    InvalidProviderOperationTransitionError,
    ProviderCostSummary,
    ProviderOperation,
    ProviderOperationErrorCode,
    ProviderOperationKind,
    ProviderOperationRelationshipError,
    ProviderOperationScopeError,
    ProviderOperationState,
)


class ProviderOperationRow(Base):
    """Content-free canonical accounting for one bounded external provider call."""

    __tablename__ = "provider_operations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["workspace_id", "research_request_id"],
            ["research_requests.workspace_id", "research_requests.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "processing_run_id"],
            ["processing_runs.workspace_id", "processing_runs.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(operation = 'DOCUMENT_EMBEDDING' "
            "AND processing_run_id IS NOT NULL AND research_request_id IS NULL) OR "
            "(operation IN ('QUERY_EMBEDDING', 'ANSWER_GENERATION', 'STATEMENT_SUPPORT') "
            "AND research_request_id IS NOT NULL AND processing_run_id IS NULL)",
            name="coherent_owner",
        ),
        CheckConstraint(
            "operation IN ('DOCUMENT_EMBEDDING', 'QUERY_EMBEDDING', "
            "'ANSWER_GENERATION', 'STATEMENT_SUPPORT')",
            name="operation",
        ),
        CheckConstraint(
            "state IN ('STARTED', 'SUCCEEDED', 'FAILED')",
            name="state",
        ),
        CheckConstraint(
            "provider ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]*$' "
            "AND model ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]*$'",
            name="provider_identifiers",
        ),
        CheckConstraint(
            "configuration_version ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'",
            name="configuration_version",
        ),
        CheckConstraint(
            "trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0', 32)",
            name="trace_id",
        ),
        CheckConstraint(
            "reserved_cost_micro_usd >= 0 "
            "AND (actual_cost_micro_usd IS NULL OR actual_cost_micro_usd >= 0) "
            "AND (actual_cost_micro_usd IS NULL "
            "OR actual_cost_micro_usd <= reserved_cost_micro_usd)",
            name="bounded_cost",
        ),
        CheckConstraint(
            "(input_units IS NULL OR input_units >= 0) "
            "AND (output_units IS NULL OR output_units >= 0) "
            "AND (duration_ms IS NULL OR duration_ms >= 0)",
            name="non_negative_measurements",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name="ordered_completion_time",
        ),
        CheckConstraint(
            "(state = 'STARTED' AND actual_cost_micro_usd IS NULL "
            "AND input_units IS NULL AND output_units IS NULL "
            "AND completed_at IS NULL AND duration_ms IS NULL "
            "AND safe_error_code IS NULL) OR "
            "(state = 'SUCCEEDED' AND actual_cost_micro_usd IS NOT NULL "
            "AND completed_at IS NOT NULL AND duration_ms IS NOT NULL "
            "AND safe_error_code IS NULL) OR "
            "(state = 'FAILED' AND actual_cost_micro_usd IS NOT NULL "
            "AND completed_at IS NOT NULL AND duration_ms IS NOT NULL "
            "AND safe_error_code IS NOT NULL)",
            name="lifecycle",
        ),
        CheckConstraint(
            "safe_error_code IS NULL OR safe_error_code IN "
            "('AUTHENTICATION', 'RATE_LIMIT', 'TIMEOUT', 'REFUSAL', "
            "'CONTRACT', 'CONFIGURATION', 'UNAVAILABLE')",
            name="safe_error_code",
        ),
        Index(None, "workspace_id", "correlation_id", "started_at"),
        Index(None, "workspace_id", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    research_request_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    processing_run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    operation: Mapped[ProviderOperationKind] = mapped_column(
        SqlEnum(
            ProviderOperationKind,
            name="provider_operation_kind",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=32,
        ),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    configuration_version: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[ProviderOperationState] = mapped_column(
        SqlEnum(
            ProviderOperationState,
            name="provider_operation_state",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=16,
        ),
        nullable=False,
    )
    reserved_cost_micro_usd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_cost_micro_usd: Mapped[int | None] = mapped_column(BigInteger)
    input_units: Mapped[int | None] = mapped_column(BigInteger)
    output_units: Mapped[int | None] = mapped_column(BigInteger)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    safe_error_code: Mapped[ProviderOperationErrorCode | None] = mapped_column(
        SqlEnum(
            ProviderOperationErrorCode,
            name="provider_operation_error_code",
            native_enum=False,
            create_constraint=False,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=32,
        )
    )

    @classmethod
    def from_domain(cls, operation: ProviderOperation) -> Self:
        """Translate one validated provider operation into a pending row."""

        return cls(**operation.model_dump())

    def apply_finalization(self, operation: ProviderOperation) -> None:
        """Apply only the controlled terminal fields to an existing row."""

        self.state = operation.state
        self.actual_cost_micro_usd = operation.actual_cost_micro_usd
        self.input_units = operation.input_units
        self.output_units = operation.output_units
        self.completed_at = operation.completed_at
        self.duration_ms = operation.duration_ms
        self.safe_error_code = operation.safe_error_code

    def to_domain(self) -> ProviderOperation:
        """Translate a loaded row into a validated provider operation."""

        return ProviderOperation(
            id=self.id,
            workspace_id=self.workspace_id,
            correlation_id=self.correlation_id,
            research_request_id=self.research_request_id,
            processing_run_id=self.processing_run_id,
            operation=self.operation,
            provider=self.provider,
            model=self.model,
            configuration_version=self.configuration_version,
            state=self.state,
            reserved_cost_micro_usd=self.reserved_cost_micro_usd,
            actual_cost_micro_usd=self.actual_cost_micro_usd,
            input_units=self.input_units,
            output_units=self.output_units,
            started_at=self.started_at,
            completed_at=self.completed_at,
            duration_ms=self.duration_ms,
            trace_id=self.trace_id,
            safe_error_code=self.safe_error_code,
        )


class SqlAlchemyProviderOperationRepository:
    """Provider-operation repository bound to one caller-owned SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def acquire_budget_lock(self, context: WorkspaceContext) -> None:
        """Serialize one workspace's budget check for the current transaction."""

        digest = blake2b(
            context.workspace_id.bytes,
            digest_size=8,
            person=b"m1-budget",
        ).digest()
        lock_key = int.from_bytes(digest, byteorder="big", signed=True)
        self._session.execute(select(func.pg_advisory_xact_lock(lock_key)))

    def add(self, context: WorkspaceContext, operation: ProviderOperation) -> None:
        """Stage one STARTED provider operation inside the active workspace."""

        self._require_scope(context, operation.workspace_id)
        if operation.state is not ProviderOperationState.STARTED:
            raise ProviderOperationRelationshipError(
                "new provider operation must start in STARTED state"
            )
        self._session.add(ProviderOperationRow.from_domain(operation))

    def get_by_id(
        self,
        context: WorkspaceContext,
        operation_id: UUID,
    ) -> ProviderOperation | None:
        """Return an operation from the trusted workspace only."""

        row = self._session.scalar(
            select(ProviderOperationRow).where(
                ProviderOperationRow.workspace_id == context.workspace_id,
                ProviderOperationRow.id == operation_id,
            )
        )
        return None if row is None else row.to_domain()

    def finalize(self, context: WorkspaceContext, operation: ProviderOperation) -> bool:
        """Advance one existing STARTED operation to a terminal state."""

        self._require_scope(context, operation.workspace_id)
        if operation.state is ProviderOperationState.STARTED:
            raise InvalidProviderOperationTransitionError(
                "provider operation finalization requires a terminal state"
            )

        row = self._session.scalar(
            select(ProviderOperationRow)
            .where(
                ProviderOperationRow.workspace_id == context.workspace_id,
                ProviderOperationRow.id == operation.id,
            )
            .with_for_update()
        )
        if row is None:
            return False
        if (
            row.correlation_id != operation.correlation_id
            or row.research_request_id != operation.research_request_id
            or row.processing_run_id != operation.processing_run_id
            or row.operation != operation.operation
            or row.provider != operation.provider
            or row.model != operation.model
            or row.configuration_version != operation.configuration_version
            or row.reserved_cost_micro_usd != operation.reserved_cost_micro_usd
            or row.started_at != operation.started_at
            or row.trace_id != operation.trace_id
        ):
            raise ProviderOperationRelationshipError(
                "provider operation identity or configuration changed"
            )
        if not row.state.can_transition_to(operation.state):
            raise InvalidProviderOperationTransitionError(
                f"provider operation cannot transition from {row.state} to {operation.state}"
            )

        row.apply_finalization(operation)
        return True

    def list_by_correlation_id(
        self,
        context: WorkspaceContext,
        correlation_id: UUID,
    ) -> Sequence[ProviderOperation]:
        """Return one workspace's operations in stable start order."""

        rows = self._session.scalars(
            select(ProviderOperationRow)
            .where(
                ProviderOperationRow.workspace_id == context.workspace_id,
                ProviderOperationRow.correlation_id == correlation_id,
            )
            .order_by(ProviderOperationRow.started_at, ProviderOperationRow.id)
        )
        return tuple(row.to_domain() for row in rows)

    def summarize_cost(
        self,
        context: WorkspaceContext,
        *,
        started_at_from: datetime,
        started_at_before: datetime,
    ) -> ProviderCostSummary:
        """Sum terminal actual cost and active reservations for a half-open UTC window."""

        if started_at_from.tzinfo is None or started_at_before.tzinfo is None:
            raise ValueError("cost-summary boundaries must be timezone-aware")
        window_start = started_at_from.astimezone(UTC)
        window_end = started_at_before.astimezone(UTC)
        if window_end <= window_start:
            raise ValueError("cost-summary window must have positive duration")

        actual_cost, active_reservations = self._session.execute(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (
                                ProviderOperationRow.state.in_(
                                    (
                                        ProviderOperationState.SUCCEEDED,
                                        ProviderOperationState.FAILED,
                                    )
                                ),
                                ProviderOperationRow.actual_cost_micro_usd,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                ProviderOperationRow.state == ProviderOperationState.STARTED,
                                ProviderOperationRow.reserved_cost_micro_usd,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
            ).where(
                ProviderOperationRow.workspace_id == context.workspace_id,
                ProviderOperationRow.started_at >= window_start,
                ProviderOperationRow.started_at < window_end,
            )
        ).one()
        return ProviderCostSummary(
            actual_cost_micro_usd=int(actual_cost),
            active_reserved_cost_micro_usd=int(active_reservations),
        )

    @staticmethod
    def _require_scope(context: WorkspaceContext, workspace_id: UUID) -> None:
        """Reject a cross-workspace write before accessing persistence state."""

        if workspace_id != context.workspace_id:
            raise ProviderOperationScopeError("provider operation is outside the active workspace")
