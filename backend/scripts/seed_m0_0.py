"""Seed the deterministic synthetic workspaces required by M0.0."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from investment_research.infrastructure.database.session import (
    create_database_engine,
    create_session_factory,
    transactional_session,
)
from investment_research.modules.identity.application import (
    LOCAL_DEVELOPMENT_USER_ID,
    LOCAL_DEVELOPMENT_WORKSPACE_ID,
)
from investment_research.modules.identity.domain import (
    Workspace,
    WorkspaceContext,
    WorkspaceType,
)
from investment_research.modules.identity.persistence import SqlAlchemyWorkspaceRepository
from investment_research.settings import load_database_settings

ISOLATION_TEST_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000003")
SEED_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)

M0_0_WORKSPACES = (
    Workspace(
        id=LOCAL_DEVELOPMENT_WORKSPACE_ID,
        name="Local Research Demo",
        workspace_type=WorkspaceType.SANITIZED_DEMO,
        created_at=SEED_CREATED_AT,
    ),
    Workspace(
        id=ISOLATION_TEST_WORKSPACE_ID,
        name="Isolation Test Workspace",
        workspace_type=WorkspaceType.SANITIZED_DEMO,
        created_at=SEED_CREATED_AT,
    ),
)


class SeedConflictError(RuntimeError):
    """Raised when a deterministic seed ID already contains different data."""


@dataclass(frozen=True, slots=True)
class SeedResult:
    """Counts produced by one idempotent seed execution."""

    created: int
    unchanged: int


def seed_m0_0_workspaces(session: Session) -> SeedResult:
    """Create missing synthetic workspaces and reject conflicting existing rows."""

    repository = SqlAlchemyWorkspaceRepository(session)
    created = 0
    unchanged = 0

    for workspace in M0_0_WORKSPACES:
        context = WorkspaceContext(
            user_id=LOCAL_DEVELOPMENT_USER_ID,
            workspace_id=workspace.id,
        )
        existing = repository.get_by_id(context, workspace.id)

        if existing is None:
            repository.add(context, workspace)
            created += 1
            continue
        if existing != workspace:
            raise SeedConflictError(
                f"workspace {workspace.id} exists with data that differs from the M0.0 seed"
            )
        unchanged += 1

    return SeedResult(created=created, unchanged=unchanged)


def seed_database(engine: Engine) -> SeedResult:
    """Run the complete M0.0 seed as one transaction."""

    session_factory = create_session_factory(engine)
    with transactional_session(session_factory) as session:
        return seed_m0_0_workspaces(session)


def main() -> int:
    """Load validated local settings, seed PostgreSQL, and report the outcome."""

    engine = create_database_engine(load_database_settings())
    try:
        result = seed_database(engine)
    finally:
        engine.dispose()

    print(f"M0.0 workspaces: {result.created} created, {result.unchanged} unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
