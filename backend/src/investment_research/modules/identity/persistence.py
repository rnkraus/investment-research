"""SQLAlchemy mapping for identity and workspace persistence."""

from datetime import datetime
from typing import Self
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, String, func, select
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, Session, mapped_column

from investment_research.infrastructure.database.base import Base
from investment_research.modules.identity.domain import (
    Workspace,
    WorkspaceContext,
    WorkspaceScopeError,
    WorkspaceType,
)


class WorkspaceRow(Base):
    """Persisted workspace authorization boundary."""

    __tablename__ = "workspaces"
    __table_args__ = (CheckConstraint("char_length(btrim(name)) > 0", name="non_empty_name"),)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    workspace_type: Mapped[WorkspaceType] = mapped_column(
        SqlEnum(
            WorkspaceType,
            name="workspace_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=32,
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    @classmethod
    def from_domain(cls, workspace: Workspace) -> Self:
        """Translate an immutable domain workspace into a pending database row."""

        return cls(
            id=workspace.id,
            name=workspace.name,
            workspace_type=workspace.workspace_type,
            created_at=workspace.created_at,
        )

    def to_domain(self) -> Workspace:
        """Translate a loaded database row into an immutable domain workspace."""

        return Workspace(
            id=self.id,
            name=self.name,
            workspace_type=self.workspace_type,
            created_at=self.created_at,
        )


class SqlAlchemyWorkspaceRepository:
    """Workspace repository bound to one caller-owned SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, context: WorkspaceContext, workspace: Workspace) -> None:
        """Stage a workspace only when it matches the trusted write scope."""

        if context.workspace_id != workspace.id:
            raise WorkspaceScopeError("workspace write is outside the active scope")
        self._session.add(WorkspaceRow.from_domain(workspace))

    def get_by_id(
        self,
        context: WorkspaceContext,
        workspace_id: UUID,
    ) -> Workspace | None:
        """Return a workspace only when the requested ID is in the active scope."""

        if context.workspace_id != workspace_id:
            return None

        row = self._session.scalar(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
        return None if row is None else row.to_domain()
