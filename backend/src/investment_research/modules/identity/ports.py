"""Application-owned persistence contracts for identity and workspaces."""

from typing import Protocol
from uuid import UUID

from investment_research.modules.identity.domain import Workspace, WorkspaceContext


class WorkspaceRepository(Protocol):
    """Store and retrieve workspaces without exposing persistence details."""

    def add(self, context: WorkspaceContext, workspace: Workspace) -> None:
        """Stage a workspace whose identifier matches the trusted context."""

    def get_by_id(
        self,
        context: WorkspaceContext,
        workspace_id: UUID,
    ) -> Workspace | None:
        """Return a scoped workspace, hiding inaccessible and unknown identifiers alike."""
