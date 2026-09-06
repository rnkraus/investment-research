"""Tests for the server-owned local identity boundary."""

from typing import TypedDict
from uuid import UUID

from fastapi.testclient import TestClient

from investment_research.api.dependencies import WorkspaceContextDependency
from investment_research.main import create_app
from investment_research.modules.identity.application import (
    LOCAL_DEVELOPMENT_USER_ID,
    LOCAL_DEVELOPMENT_WORKSPACE_ID,
)
from investment_research.settings import Settings

LOCAL_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:5432/test-database"


class ContextResponse(TypedDict):
    user_id: UUID
    workspace_id: UUID


def test_client_workspace_id_cannot_override_server_context() -> None:
    application = create_app(
        Settings.model_validate({"environment": "test", "database_url": LOCAL_DATABASE_URL})
    )

    @application.get("/_test/context")
    def read_context(context: WorkspaceContextDependency) -> ContextResponse:
        return {
            "user_id": context.user_id,
            "workspace_id": context.workspace_id,
        }

    attacker_workspace_id = UUID("00000000-0000-4000-8000-000000000099")

    with TestClient(application) as client:
        response = client.get(
            "/_test/context",
            params={"workspace_id": str(attacker_workspace_id)},
        )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(LOCAL_DEVELOPMENT_USER_ID),
        "workspace_id": str(LOCAL_DEVELOPMENT_WORKSPACE_ID),
    }
    assert attacker_workspace_id != LOCAL_DEVELOPMENT_WORKSPACE_ID
