"""Unit tests for the deterministic M0.0 workspace seed."""

from unittest.mock import create_autospec

import pytest
from sqlalchemy.orm import Session

from investment_research.modules.identity.application import LOCAL_DEVELOPMENT_WORKSPACE_ID
from investment_research.modules.identity.persistence import WorkspaceRow
from scripts.seed_m0_0 import (
    M0_0_WORKSPACES,
    SeedConflictError,
    SeedResult,
    seed_m0_0_workspaces,
)


def test_seed_uses_the_runtime_workspace_and_stages_both_missing_workspaces() -> None:
    session = create_autospec(Session, instance=True)
    session.scalar.side_effect = [None, None]

    result = seed_m0_0_workspaces(session)

    assert M0_0_WORKSPACES[0].id == LOCAL_DEVELOPMENT_WORKSPACE_ID
    assert result == SeedResult(created=2, unchanged=0)
    assert [call.args[0].to_domain() for call in session.add.call_args_list] == list(
        M0_0_WORKSPACES
    )


def test_seed_leaves_identical_existing_workspaces_unchanged() -> None:
    session = create_autospec(Session, instance=True)
    session.scalar.side_effect = [
        WorkspaceRow.from_domain(workspace) for workspace in M0_0_WORKSPACES
    ]

    result = seed_m0_0_workspaces(session)

    assert result == SeedResult(created=0, unchanged=2)
    session.add.assert_not_called()


def test_seed_rejects_existing_workspace_with_conflicting_data() -> None:
    session = create_autospec(Session, instance=True)
    conflicting_workspace = M0_0_WORKSPACES[0].model_copy(update={"name": "Unexpected Name"})
    session.scalar.return_value = WorkspaceRow.from_domain(conflicting_workspace)

    with pytest.raises(SeedConflictError, match=r"differs from the M0\.0 seed"):
        seed_m0_0_workspaces(session)

    session.add.assert_not_called()
