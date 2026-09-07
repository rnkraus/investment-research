"""PostgreSQL integration tests for the deterministic M0.0 workspace seed."""

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from investment_research.modules.identity.persistence import WorkspaceRow
from scripts.seed_m0_0 import M0_0_WORKSPACES, SeedResult, seed_database


def test_seed_creates_exact_workspaces_and_is_idempotent(
    integration_database_engine: Engine,
) -> None:
    first_result = seed_database(integration_database_engine)
    second_result = seed_database(integration_database_engine)

    with Session(integration_database_engine) as session:
        rows = session.scalars(
            select(WorkspaceRow)
            .where(WorkspaceRow.id.in_(workspace.id for workspace in M0_0_WORKSPACES))
            .order_by(WorkspaceRow.id)
        ).all()

    expected_workspaces = sorted(M0_0_WORKSPACES, key=lambda workspace: workspace.id)

    assert first_result.created + first_result.unchanged == len(M0_0_WORKSPACES)
    assert second_result == SeedResult(created=0, unchanged=2)
    assert [row.to_domain() for row in rows] == expected_workspaces
