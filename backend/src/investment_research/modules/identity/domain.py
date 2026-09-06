"""Identity and workspace values that carry the authorization boundary."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class WorkspaceType(StrEnum):
    """Supported privacy classification for a workspace."""

    PRIVATE = "private"
    SANITIZED_DEMO = "sanitized_demo"


class WorkspaceScopeError(ValueError):
    """Raised when a write targets a workspace outside the trusted context."""


class Workspace(BaseModel):
    """Immutable tenant and authorization boundary stored by the application."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    id: UUID
    name: str = Field(min_length=1, max_length=120)
    workspace_type: WorkspaceType
    created_at: AwareDatetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at_to_utc(cls, value: datetime) -> datetime:
        """Store one canonical instant representation at the domain boundary."""

        return value.astimezone(UTC)


class WorkspaceContext(BaseModel):
    """Server-resolved user and workspace scope for one operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: UUID
    workspace_id: UUID
