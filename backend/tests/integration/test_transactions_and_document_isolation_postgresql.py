"""PostgreSQL transaction and document workspace-isolation tests."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import Engine, delete, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.modules.documents.domain import (
    Document,
    DocumentLifecycleState,
    DocumentVersion,
    OriginalArtifact,
    OriginalMediaType,
)
from investment_research.modules.documents.persistence import (
    DocumentVersionRow,
    OriginalArtifactRow,
    SqlAlchemyDocumentRepository,
)
from investment_research.modules.identity.domain import (
    Workspace,
    WorkspaceContext,
    WorkspaceType,
)
from investment_research.modules.identity.persistence import SqlAlchemyWorkspaceRepository

FIXTURE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
FIXTURE_SHA256 = "a" * 64


def make_workspace() -> tuple[WorkspaceContext, Workspace]:
    """Create one unique synthetic workspace and its trusted context."""

    workspace_id = uuid4()
    return (
        WorkspaceContext(user_id=uuid4(), workspace_id=workspace_id),
        Workspace(
            id=workspace_id,
            name=f"Integration Workspace {workspace_id}",
            workspace_type=WorkspaceType.SANITIZED_DEMO,
            created_at=FIXTURE_TIME,
        ),
    )


def make_capture(
    context: WorkspaceContext,
) -> tuple[Document, DocumentVersion, OriginalArtifact]:
    """Create one internally consistent document capture in the given workspace."""

    document_id = uuid4()
    version_id = uuid4()
    original_id = uuid4()
    document = Document(
        id=document_id,
        workspace_id=context.workspace_id,
        document_type="earnings_brief",
        lifecycle_state=DocumentLifecycleState.ACTIVE,
        current_version_id=version_id,
        created_at=FIXTURE_TIME,
    )
    version = DocumentVersion(
        id=version_id,
        workspace_id=context.workspace_id,
        document_id=document_id,
        version_number=1,
        title=f"Synthetic earnings brief {document_id}",
        language="en",
        content_sha256=FIXTURE_SHA256,
        published_at=FIXTURE_TIME,
        available_at=FIXTURE_TIME,
        captured_at=FIXTURE_TIME,
    )
    original = OriginalArtifact(
        id=original_id,
        workspace_id=context.workspace_id,
        document_version_id=version_id,
        storage_key=f"workspaces/{context.workspace_id}/originals/{original_id}.pdf",
        media_type=OriginalMediaType.PDF,
        byte_size=1_024,
        checksum_sha256=FIXTURE_SHA256,
        captured_at=FIXTURE_TIME,
    )
    return document, version, original


def test_transaction_commits_success_and_rolls_back_failure(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    committed_context, committed_workspace = make_workspace()
    rolled_back_context, rolled_back_workspace = make_workspace()

    with transactional_session(session_factory) as session:
        SqlAlchemyWorkspaceRepository(session).add(committed_context, committed_workspace)

    with (
        pytest.raises(RuntimeError, match="force PostgreSQL rollback"),
        transactional_session(session_factory) as session,
    ):
        SqlAlchemyWorkspaceRepository(session).add(rolled_back_context, rolled_back_workspace)
        raise RuntimeError("force PostgreSQL rollback")

    with transactional_session(session_factory) as session:
        repository = SqlAlchemyWorkspaceRepository(session)
        assert (
            repository.get_by_id(committed_context, committed_workspace.id) == committed_workspace
        )
        assert repository.get_by_id(rolled_back_context, rolled_back_workspace.id) is None


def test_document_repository_does_not_disclose_another_workspace(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    first_context, first_workspace = make_workspace()
    second_context, second_workspace = make_workspace()
    first_capture = make_capture(first_context)
    second_capture = make_capture(second_context)

    with transactional_session(session_factory) as session:
        workspace_repository = SqlAlchemyWorkspaceRepository(session)
        workspace_repository.add(first_context, first_workspace)
        workspace_repository.add(second_context, second_workspace)

    with transactional_session(session_factory) as session:
        document_repository = SqlAlchemyDocumentRepository(session)
        document_repository.add_capture(first_context, *first_capture)
        document_repository.add_capture(second_context, *second_capture)

    first_document, first_version, first_original = first_capture
    second_document, second_version, second_original = second_capture

    with transactional_session(session_factory) as session:
        repository = SqlAlchemyDocumentRepository(session)

        assert repository.get_document_by_id(first_context, first_document.id) == first_document
        assert (
            repository.get_current_version_by_id(
                first_context,
                first_document.id,
                first_version.id,
            )
            == first_version
        )
        assert (
            repository.get_original_artifact(
                first_context,
                first_document.id,
                first_version.id,
            )
            == first_original
        )

        assert repository.get_document_by_id(first_context, second_document.id) is None
        assert (
            repository.get_current_version_by_id(
                first_context,
                second_document.id,
                second_version.id,
            )
            is None
        )
        assert (
            repository.get_original_artifact(
                first_context,
                second_document.id,
                second_version.id,
            )
            is None
        )

        assert repository.get_document_by_id(second_context, second_document.id) == second_document
        assert (
            repository.get_current_version_by_id(
                second_context,
                second_document.id,
                second_version.id,
            )
            == second_version
        )
        assert (
            repository.get_original_artifact(
                second_context,
                second_document.id,
                second_version.id,
            )
            == second_original
        )


def test_database_rejects_document_version_linked_across_workspaces(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    document_context, document_workspace = make_workspace()
    foreign_context, foreign_workspace = make_workspace()
    document, version, original = make_capture(document_context)

    with transactional_session(session_factory) as session:
        workspace_repository = SqlAlchemyWorkspaceRepository(session)
        workspace_repository.add(document_context, document_workspace)
        workspace_repository.add(foreign_context, foreign_workspace)

    with transactional_session(session_factory) as session:
        SqlAlchemyDocumentRepository(session).add_capture(
            document_context,
            document,
            version,
            original,
        )

    cross_workspace_version = version.model_copy(
        update={
            "id": uuid4(),
            "workspace_id": foreign_context.workspace_id,
            "version_number": 2,
        }
    )

    with (
        pytest.raises(IntegrityError),
        transactional_session(session_factory) as session,
    ):
        session.add(DocumentVersionRow.from_domain(cross_workspace_version))


def test_database_check_constraint_rejects_invalid_workspace_type(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    invalid_workspace_id = uuid4()

    with (
        pytest.raises(IntegrityError),
        transactional_session(session_factory) as session,
    ):
        session.execute(
            text(
                "INSERT INTO workspaces (id, name, workspace_type) "
                "VALUES (:id, :name, :workspace_type)"
            ),
            {
                "id": invalid_workspace_id,
                "name": "Invalid Workspace Type",
                "workspace_type": "not_a_workspace_type",
            },
        )

    context = WorkspaceContext(user_id=uuid4(), workspace_id=invalid_workspace_id)
    with transactional_session(session_factory) as session:
        assert (
            SqlAlchemyWorkspaceRepository(session).get_by_id(
                context,
                invalid_workspace_id,
            )
            is None
        )


def test_database_triggers_keep_versions_and_originals_immutable(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    context, workspace = make_workspace()
    document, version, original = make_capture(context)

    with transactional_session(session_factory) as session:
        SqlAlchemyWorkspaceRepository(session).add(context, workspace)

    with transactional_session(session_factory) as session:
        SqlAlchemyDocumentRepository(session).add_capture(
            context,
            document,
            version,
            original,
        )

    with (
        pytest.raises(DBAPIError),
        transactional_session(session_factory) as session,
    ):
        session.execute(
            update(DocumentVersionRow)
            .where(DocumentVersionRow.id == version.id)
            .values(title="Changed title")
        )

    with (
        pytest.raises(DBAPIError),
        transactional_session(session_factory) as session,
    ):
        session.execute(delete(OriginalArtifactRow).where(OriginalArtifactRow.id == original.id))

    with transactional_session(session_factory) as session:
        repository = SqlAlchemyDocumentRepository(session)
        assert repository.get_current_version_by_id(context, document.id, version.id) == version
        assert repository.get_original_artifact(context, document.id, version.id) == original
