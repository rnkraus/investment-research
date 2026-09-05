"""Identity values that carry the authorization boundary."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class WorkspaceContext(BaseModel):
    """Server-resolved user and workspace scope for one operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: UUID
    workspace_id: UUID
