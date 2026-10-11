"""Domain values and lifecycle invariants for canonical provider operations."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

ProviderIdentifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
ConfigurationVersion = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
TraceId = Annotated[
    str,
    StringConstraints(
        to_lower=True,
        min_length=32,
        max_length=32,
        pattern=r"^[0-9a-fA-F]{32}$",
    ),
]
MicroUsd = Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807)]
UsageUnits = Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807)]
DurationMilliseconds = Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807)]


class ProviderOperationKind(StrEnum):
    """External provider operations attributable during M1 and the M1.5 experiment."""

    DOCUMENT_EMBEDDING = "DOCUMENT_EMBEDDING"
    QUERY_EMBEDDING = "QUERY_EMBEDDING"
    RERANKING = "RERANKING"
    ANSWER_GENERATION = "ANSWER_GENERATION"
    STATEMENT_SUPPORT = "STATEMENT_SUPPORT"


class ProviderOperationState(StrEnum):
    """Controlled lifecycle for one canonical provider-operation record."""

    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"

    def can_transition_to(self, target: Self) -> bool:
        """Allow one forward-only finalization from STARTED."""

        return self is ProviderOperationState.STARTED and target in {
            ProviderOperationState.SUCCEEDED,
            ProviderOperationState.FAILED,
        }


class ProviderOperationErrorCode(StrEnum):
    """Safe internal failure categories that contain no provider details."""

    AUTHENTICATION = "AUTHENTICATION"
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    REFUSAL = "REFUSAL"
    CONTRACT = "CONTRACT"
    CONFIGURATION = "CONFIGURATION"
    UNAVAILABLE = "UNAVAILABLE"


class ProviderOperationScopeError(ValueError):
    """Raised when an operation crosses the trusted workspace boundary."""


class ProviderOperationRelationshipError(ValueError):
    """Raised when operation identifiers do not describe one coherent call."""


class InvalidProviderOperationTransitionError(ValueError):
    """Raised when a provider operation skips or reverses its lifecycle."""


class ProviderCostSummary(BaseModel):
    """Workspace cost already spent plus active reservations for one time window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    actual_cost_micro_usd: MicroUsd
    active_reserved_cost_micro_usd: MicroUsd

    @property
    def accounted_cost_micro_usd(self) -> int:
        """Return committed actual cost plus reservations that are still active."""

        return self.actual_cost_micro_usd + self.active_reserved_cost_micro_usd


class ProviderUsage(BaseModel):
    """Provider-reported bounded input and output units for one external call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_units: UsageUnits
    output_units: UsageUnits

    @property
    def total_units(self) -> int:
        """Return provider input and output units without floating-point arithmetic."""

        return self.input_units + self.output_units


class ProviderOperation(BaseModel):
    """Canonical, content-free accounting record for one external provider call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    workspace_id: UUID
    correlation_id: UUID
    research_request_id: UUID | None = None
    processing_run_id: UUID | None = None
    owner_deleted: bool = False
    operation: ProviderOperationKind
    provider: ProviderIdentifier
    model: ProviderIdentifier
    configuration_version: ConfigurationVersion
    state: ProviderOperationState
    reserved_cost_micro_usd: MicroUsd
    actual_cost_micro_usd: MicroUsd | None = None
    input_units: UsageUnits | None = None
    output_units: UsageUnits | None = None
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    duration_ms: DurationMilliseconds | None = None
    trace_id: TraceId
    safe_error_code: ProviderOperationErrorCode | None = None

    @field_validator("started_at", "completed_at")
    @classmethod
    def normalize_times(cls, value: datetime | None) -> datetime | None:
        """Normalize operation timestamps to UTC."""

        return None if value is None else value.astimezone(UTC)

    @field_validator("trace_id")
    @classmethod
    def require_nonzero_trace_id(cls, value: str) -> str:
        """Reject the invalid all-zero W3C trace identifier."""

        if value == "0" * 32:
            raise ValueError("trace_id must not be all zeros")
        return value

    @model_validator(mode="after")
    def require_coherent_operation(self) -> Self:
        """Keep ownership, operation kind, lifecycle, timing, and cost coherent."""

        has_research_request = self.research_request_id is not None
        has_processing_run = self.processing_run_id is not None
        if self.owner_deleted:
            if has_research_request or has_processing_run:
                raise ValueError("deleted operation owner must not retain source relationships")
        elif has_research_request == has_processing_run:
            raise ValueError("exactly one of research_request_id or processing_run_id is required")

        research_operations = {
            ProviderOperationKind.QUERY_EMBEDDING,
            ProviderOperationKind.RERANKING,
            ProviderOperationKind.ANSWER_GENERATION,
            ProviderOperationKind.STATEMENT_SUPPORT,
        }
        if (
            not self.owner_deleted
            and self.operation in research_operations
            and not has_research_request
        ):
            raise ValueError("research provider operation requires research_request_id")
        if (
            not self.owner_deleted
            and self.operation is ProviderOperationKind.DOCUMENT_EMBEDDING
            and not has_processing_run
        ):
            raise ValueError("document embedding requires processing_run_id")

        if self.actual_cost_micro_usd is not None and (
            self.actual_cost_micro_usd > self.reserved_cost_micro_usd
        ):
            raise ValueError("actual cost must not exceed the reserved cost")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")

        terminal_values = (
            self.actual_cost_micro_usd,
            self.completed_at,
            self.duration_ms,
        )
        if self.state is ProviderOperationState.STARTED:
            if any(value is not None for value in terminal_values):
                raise ValueError("STARTED operation must not contain completion values")
            if (
                self.input_units is not None
                or self.output_units is not None
                or self.safe_error_code is not None
            ):
                raise ValueError("STARTED operation must not contain provider results")
        elif any(value is None for value in terminal_values):
            raise ValueError("terminal operation requires cost, completion time, and duration")

        if self.state is ProviderOperationState.FAILED:
            if self.safe_error_code is None:
                raise ValueError("FAILED operation requires safe_error_code")
        elif self.safe_error_code is not None:
            raise ValueError("safe_error_code is allowed only for FAILED operations")

        return self
