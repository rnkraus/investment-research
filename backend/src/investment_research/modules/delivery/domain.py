"""Content-free, deterministic outbox intent and bounded delivery metadata."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite
from typing import Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

OUTBOX_SCHEMA_VERSION = "outbox-event.v1"
OUTBOX_MAX_GENERATIONS = 3
OUTBOX_MAX_ATTEMPTS = 3
OUTBOX_CLAIM_SECONDS = 30
OUTBOX_MAX_RETRY_DELAY_SECONDS = 30
OUTBOX_UNSTARTED_GRACE_SECONDS = 420


class OutboxTransitionError(ValueError):
    """Reject a premature, exhausted, or no-longer-owned delivery transition."""


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

    def claim(self, *, lease_id: UUID, claimed_at: datetime) -> Self:
        """Consume one publication attempt, reclaiming only an expired delivery fence."""

        claimed_at = self._aware_time(claimed_at)
        due_at = (
            self.lease_expires_at if self.state is OutboxState.CLAIMED else self.next_attempt_at
        )
        if (
            self.state not in {OutboxState.PENDING, OutboxState.RETRY_WAIT, OutboxState.CLAIMED}
            or due_at is None
            or due_at > claimed_at
            or self.attempt_count >= OUTBOX_MAX_ATTEMPTS
        ):
            raise OutboxTransitionError("delivery intent is not claimable")
        return self._replace(
            state=OutboxState.CLAIMED,
            attempt_count=self.attempt_count + 1,
            next_attempt_at=None,
            lease_id=lease_id,
            lease_expires_at=claimed_at + timedelta(seconds=OUTBOX_CLAIM_SECONDS),
        )

    def redeliver_unstarted(self, *, now: datetime) -> Self:
        """Derive new intent; the caller must first prove canonical unstarted eligibility.

        Persist this separately under scoped uniqueness, retaining this event unchanged.
        This value transition cannot authorize source access or reset execution attempts.
        """

        now = self._aware_time(now)
        if (
            self.state is not OutboxState.PUBLISHED
            or self.published_at is None
            or now < self.published_at + timedelta(seconds=OUTBOX_UNSTARTED_GRACE_SECONDS)
            or self.generation + 1 >= OUTBOX_MAX_GENERATIONS
        ):
            raise OutboxTransitionError("published delivery is not eligible for a new generation")
        return type(self)(
            event_id=outbox_event_id(
                self.workspace_id, self.kind, self.target_id, self.generation + 1
            ),
            workspace_id=self.workspace_id,
            kind=self.kind,
            target_id=self.target_id,
            generation=self.generation + 1,
            created_at=now,
            next_attempt_at=now,
        )

    def record_publication(self, *, lease_id: UUID, completed_at: datetime) -> Self:
        """Acknowledge publication, never processing success, under an unexpired fence."""

        completed_at = self._require_owner(lease_id, completed_at)
        return self._replace(
            state=OutboxState.PUBLISHED,
            lease_id=None,
            lease_expires_at=None,
            published_at=completed_at,
            last_error_category=None,
        )

    def record_failure(
        self,
        *,
        lease_id: UUID,
        completed_at: datetime,
        category: DeliveryErrorCategory,
        retry_delay_seconds: float,
    ) -> Self:
        """Persist a safe failure without resetting any delivery or execution allowance."""

        completed_at = self._require_owner(lease_id, completed_at)
        if (
            not isfinite(retry_delay_seconds)
            or not 0 <= retry_delay_seconds <= OUTBOX_MAX_RETRY_DELAY_SECONDS
        ):
            raise OutboxTransitionError("delivery retry delay is outside its bound")
        if category is DeliveryErrorCategory.SOURCE_INELIGIBLE:
            return self.cancel()
        retryable = (
            category is DeliveryErrorCategory.DEPENDENCY_UNAVAILABLE
            and self.attempt_count < OUTBOX_MAX_ATTEMPTS
        )
        return self._replace(
            state=OutboxState.RETRY_WAIT if retryable else OutboxState.BLOCKED,
            lease_id=None,
            lease_expires_at=None,
            next_attempt_at=(
                completed_at + timedelta(seconds=retry_delay_seconds) if retryable else None
            ),
            last_error_category=(
                DeliveryErrorCategory.ATTEMPTS_EXHAUSTED
                if category is DeliveryErrorCategory.DEPENDENCY_UNAVAILABLE and not retryable
                else category
            ),
        )

    def block_expired_final_claim(self, *, now: datetime) -> Self:
        """Retain the last consumed attempt after a publisher disappears."""

        now = self._aware_time(now)
        if (
            self.state is not OutboxState.CLAIMED
            or self.lease_expires_at is None
            or self.lease_expires_at > now
            or self.attempt_count != OUTBOX_MAX_ATTEMPTS
        ):
            raise OutboxTransitionError("delivery intent is not exhausted")
        return self._replace(
            state=OutboxState.BLOCKED,
            lease_id=None,
            lease_expires_at=None,
            last_error_category=DeliveryErrorCategory.ATTEMPTS_EXHAUSTED,
        )

    def cancel(self) -> Self:
        """Close redundant or ineligible publication intent without altering the run."""

        return self._replace(
            state=OutboxState.CANCELLED,
            lease_id=None,
            lease_expires_at=None,
            next_attempt_at=None,
            last_error_category=DeliveryErrorCategory.SOURCE_INELIGIBLE,
        )

    def _require_owner(self, lease_id: UUID, completed_at: datetime) -> datetime:
        completed_at = self._aware_time(completed_at)
        if (
            self.state is not OutboxState.CLAIMED
            or self.lease_id != lease_id
            or self.lease_expires_at is None
            or completed_at >= self.lease_expires_at
            or completed_at < self.lease_expires_at - timedelta(seconds=OUTBOX_CLAIM_SECONDS)
        ):
            raise OutboxTransitionError("delivery claim is no longer owned")
        return completed_at

    @staticmethod
    def _aware_time(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise OutboxTransitionError("delivery time must be timezone-aware")
        return value.astimezone(UTC)

    def _replace(self, **changes: object) -> Self:
        return self.model_validate(self.model_dump() | changes)
