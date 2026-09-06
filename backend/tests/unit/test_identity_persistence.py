"""Unit tests for workspace domain rules and scoped persistence."""

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from investment_research.modules.identity.domain import (
    Workspace,
    WorkspaceContext,
    WorkspaceScopeError,
    WorkspaceType,
)
from investment_research.modules.identity.persistence import (
    SqlAlchemyWorkspaceRepository,
    WorkspaceRow,
)
from investment_research.modules.identity.ports import WorkspaceRepository

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
OTHER_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000003")


def make_context(workspace_id: UUID = WORKSPACE_ID) -> WorkspaceContext:
    """Build a deterministic trusted context for one test workspace."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=workspace_id)


def make_workspace(workspace_id: UUID = WORKSPACE_ID) -> Workspace:
    """Build a deterministic sanitized workspace for repository tests."""

    return Workspace(
        id=workspace_id,
        name="Synthetic Research",
        workspace_type=WorkspaceType.SANITIZED_DEMO,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_workspace_normalizes_text_and_timestamp_to_utc() -> None:
    workspace = Workspace(
        id=WORKSPACE_ID,
        name="  Synthetic Research  ",
        workspace_type=WorkspaceType.SANITIZED_DEMO,
        created_at=datetime(2026, 1, 1, 2, tzinfo=timezone(timedelta(hours=2))),
    )

    assert workspace.name == "Synthetic Research"
    assert workspace.created_at == datetime(2026, 1, 1, tzinfo=UTC)


def test_workspace_rejects_timestamp_without_timezone() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        Workspace.model_validate(
            {
                "id": WORKSPACE_ID,
                "name": "Synthetic Research",
                "workspace_type": WorkspaceType.SANITIZED_DEMO,
                "created_at": datetime(2026, 1, 1),
            }
        )


def test_repository_matches_application_owned_protocol() -> None:
    session = create_autospec(Session, instance=True)

    repository: WorkspaceRepository = SqlAlchemyWorkspaceRepository(session)

    assert isinstance(repository, SqlAlchemyWorkspaceRepository)


def test_repository_stages_workspace_in_matching_scope() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyWorkspaceRepository(session)
    workspace = make_workspace()

    repository.add(make_context(), workspace)

    session.add.assert_called_once()
    staged_row = session.add.call_args.args[0]
    assert isinstance(staged_row, WorkspaceRow)
    assert staged_row.to_domain() == workspace


def test_repository_rejects_cross_workspace_write_before_database_access() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyWorkspaceRepository(session)

    with pytest.raises(WorkspaceScopeError, match="outside the active scope"):
        repository.add(make_context(OTHER_WORKSPACE_ID), make_workspace())

    session.add.assert_not_called()


def test_repository_returns_workspace_from_matching_scope() -> None:
    session = create_autospec(Session, instance=True)
    workspace = make_workspace()
    session.scalar.return_value = WorkspaceRow.from_domain(workspace)
    repository = SqlAlchemyWorkspaceRepository(session)

    result = repository.get_by_id(make_context(), WORKSPACE_ID)

    assert result == workspace
    session.scalar.assert_called_once()


def test_repository_hides_foreign_and_unknown_workspace_ids_alike() -> None:
    session = create_autospec(Session, instance=True)
    repository = SqlAlchemyWorkspaceRepository(session)

    foreign_result = repository.get_by_id(make_context(), OTHER_WORKSPACE_ID)

    session.scalar.assert_not_called()
    session.scalar.return_value = None
    unknown_result = repository.get_by_id(make_context(), WORKSPACE_ID)

    assert foreign_result is None
    assert unknown_result is None
    session.scalar.assert_called_once()
