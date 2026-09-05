"""Application-owned identity context providers."""

from typing import Protocol
from uuid import UUID

from investment_research.modules.identity.domain import WorkspaceContext

LOCAL_DEVELOPMENT_USER_ID = UUID("00000000-0000-4000-8000-000000000001")
LOCAL_DEVELOPMENT_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")


class IdentityContextProvider(Protocol):
    """Resolve the trusted identity context for the current operation."""

    def resolve(self) -> WorkspaceContext:
        """Return a context derived from server-controlled state."""


class FixedLocalIdentityContextProvider:
    """Provide the single fixed identity allowed by the local M0.0 runtime."""

    def resolve(self) -> WorkspaceContext:
        """Return the fixed context without accepting client input."""

        return WorkspaceContext(
            user_id=LOCAL_DEVELOPMENT_USER_ID,
            workspace_id=LOCAL_DEVELOPMENT_WORKSPACE_ID,
        )
