"""Content-free, deterministic outbox intent and bounded delivery metadata."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

OUTBOX_SCHEMA_VERSION = "outbox-event.v1"
OUTBOX_MAX_GENERATIONS = 3
OUTBOX_MAX_ATTEMPTS = 3


class OutboxKind(StrEnum):
    PROCESS_DOCUMENT = "PROCESS_DOCUMENT"


class OutboxState(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RETRY_WAIT = "RETRY_WAIT"
    PUBLISHED = "PUBLISHED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class DeliveryErrorCategory(StrEnum):
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    INVALID_JOB = "INVALID_JOB"
    SOURCE_INELIGIBLE = "SOURCE_INELIGIBLE"
    ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"


def outbox_event_id(workspace_id: UUID, kind: OutboxKind, target_id: UUID, generation: int) -> UUID:
    """Keep identity stable across capture, migration, and duplicate delivery intent."""

    return uuid5(
        NAMESPACE_URL,
        f"investment-research:{OUTBOX_SCHEMA_VERSION}:{workspace_id}:{kind}:{target_id}:{generation}",
    )


class OutboxEvent(BaseModel):
    """A publication intent, never processing success or a source-access capability."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    schema_version: Literal["outbox-event.v1"] = "outbox-event.v1"
    event_id: UUID
    workspace_id: UUID
    kind: OutboxKind
    target_id: UUID
    generation: int = Field(default=0, ge=0, lt=OUTBOX_MAX_GENERATIONS)
    created_at: AwareDatetime
    state: OutboxState = OutboxState.PENDING
    attempt_count: int = Field(default=0, ge=0, le=OUTBOX_MAX_ATTEMPTS)
    next_attempt_at: AwareDatetime | None
    lease_id: UUID | None = None
    lease_expires_at: AwareDatetime | None = None
    published_at: AwareDatetime | None = None
    last_error_category: DeliveryErrorCategory | None = None

    @field_validator("created_at", "next_attempt_at", "lease_expires_at", "published_at")
    @classmethod
    def normalize_utc(cls, value: datetime | None) -> datetime | None:
        """Reject naive timestamps through AwareDatetime and preserve UTC comparisons."""

        return None if value is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def require_consistent_intent(self) -> Self:
        if self.event_id != outbox_event_id(
            self.workspace_id, self.kind, self.target_id, self.generation
        ):
            raise ValueError("outbox identity does not match its scoped target")
        for value in (self.next_attempt_at, self.published_at):
            if value is not None and value < self.created_at:
                raise ValueError("outbox timestamps must not precede creation")
        if self.state is OutboxState.CLAIMED:
            if (
                self.attempt_count < 1
                or self.lease_id is None
                or self.lease_expires_at is None
                or self.lease_expires_at <= self.created_at
                or self.next_attempt_at is not None
            ):
                raise ValueError("claimed intent requires an attempt and complete lease")
        elif self.lease_id is not None or self.lease_expires_at is not None:
            raise ValueError("only claimed intent may own a lease")
        if self.state in {OutboxState.PENDING, OutboxState.RETRY_WAIT}:
            if self.next_attempt_at is None:
                raise ValueError("due intent requires a next attempt time")
        elif self.next_attempt_at is not None:
            raise ValueError("non-due intent must not carry a next attempt time")
        if self.state is OutboxState.PENDING and (
            self.attempt_count != 0 or self.last_error_category is not None
        ):
            raise ValueError("initial pending intent must have no attempts or error")
        if self.state is OutboxState.RETRY_WAIT and (
            not 0 < self.attempt_count < OUTBOX_MAX_ATTEMPTS
            or self.last_error_category is not DeliveryErrorCategory.DEPENDENCY_UNAVAILABLE
        ):
            raise ValueError("retry wait requires a retryable failure and remaining attempts")
        if self.state is OutboxState.PUBLISHED and (
            self.published_at is None or self.attempt_count < 1
        ):
            raise ValueError("published intent requires an acknowledged attempt")
        if self.state not in {OutboxState.PUBLISHED, OutboxState.CANCELLED} and (
            self.published_at is not None
        ):
            raise ValueError("unpublished intent must not carry a publication receipt")
        if self.state is OutboxState.BLOCKED and self.last_error_category is None:
            raise ValueError("blocked intent requires a safe error category")
        return self

    @classmethod
    def for_processing(
        cls, *, workspace_id: UUID, processing_run_id: UUID, created_at: datetime
    ) -> Self:
        """Create generation zero without consuming another capture-generated identifier."""

        return cls(
            event_id=outbox_event_id(
                workspace_id, OutboxKind.PROCESS_DOCUMENT, processing_run_id, 0
            ),
            workspace_id=workspace_id,
            kind=OutboxKind.PROCESS_DOCUMENT,
            target_id=processing_run_id,
            created_at=created_at,
            next_attempt_at=created_at,
        )
