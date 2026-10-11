"""PostgreSQL proof of current-document catalog isolation and keyset pagination."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import Engine, update
from sqlalchemy.orm import Session

from investment_research.infrastructure.database.session import (
    create_session_factory,
    transactional_session,
)
from investment_research.infrastructure.database.unit_of_work import (
    create_document_read_unit_of_work_factory,
)
from investment_research.modules.documents.contracts import DocumentListQuery
from investment_research.modules.documents.domain import (
    Document,
    DocumentLifecycleState,
    DocumentVersion,
    OriginalArtifact,
    OriginalMediaType,
)
from investment_research.modules.documents.listing import ListDocumentsService
from investment_research.modules.documents.persistence import (
    DocumentRow,
    DocumentVersionRow,
    OriginalArtifactRow,
    SqlAlchemyDocumentRepository,
)
from investment_research.modules.identity.domain import Workspace, WorkspaceContext, WorkspaceType
from investment_research.modules.identity.persistence import SqlAlchemyWorkspaceRepository

CREATED_AT = datetime(2026, 10, 11, 12, tzinfo=UTC)


def add_workspace(session: Session) -> WorkspaceContext:
    """Create an independent synthetic workspace in the caller's transaction."""

    context = WorkspaceContext(user_id=uuid4(), workspace_id=uuid4())
    SqlAlchemyWorkspaceRepository(session).add(
        context,
        Workspace(
            id=context.workspace_id,
            name=f"Synthetic catalog {context.workspace_id}",
            workspace_type=WorkspaceType.SANITIZED_DEMO,
            created_at=CREATED_AT,
        ),
    )
    # Establish the parent row before staging documents with immediate workspace foreign keys.
    session.flush()
    return context


def add_document(
    session: Session,
    context: WorkspaceContext,
    *,
    created_at: datetime = CREATED_AT,
    title: str = "Synthetic catalog source",
    media_type: OriginalMediaType = OriginalMediaType.PDF,
    with_original: bool = True,
) -> Document:
    """Persist only canonical metadata; catalog reads never require object-storage bytes."""

    document = Document(
        id=uuid4(),
        workspace_id=context.workspace_id,
        document_type="earnings_brief",
        lifecycle_state=DocumentLifecycleState.ACTIVE,
        current_version_id=uuid4(),
        created_at=created_at,
    )
    version = DocumentVersion(
        id=document.current_version_id,
        workspace_id=context.workspace_id,
        document_id=document.id,
        version_number=1,
        title=title,
        language="en",
        content_sha256="a" * 64,
        captured_at=created_at,
    )
    original = OriginalArtifact(
        id=uuid4(),
        workspace_id=context.workspace_id,
        document_version_id=version.id,
        storage_key=f"workspaces/{context.workspace_id}/originals/{version.id}",
        media_type=media_type,
        byte_size=128,
        checksum_sha256="a" * 64,
        captured_at=created_at,
    )
    if with_original:
        SqlAlchemyDocumentRepository(session).add_capture(context, document, version, original)
    else:
        session.add_all(
            [DocumentRow.from_domain(document), DocumentVersionRow.from_domain(version)]
        )
    return document


def make_service(engine: Engine) -> ListDocumentsService:
    """Use the production short-lived read unit of work against migrated PostgreSQL."""

    return ListDocumentsService(
        unit_of_work_factory=create_document_read_unit_of_work_factory(
            create_session_factory(engine)
        )
    )


def test_document_list_hides_foreign_sources_and_preserves_unknown_publication_time(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    with transactional_session(session_factory) as session:
        owner = add_workspace(session)
        foreign = add_workspace(session)
        empty = add_workspace(session)
        own_document = add_document(session, owner, media_type=OriginalMediaType.PLAIN_TEXT)
        foreign_document = add_document(session, foreign, title="Foreign synthetic source")

    service = make_service(integration_database_engine)
    own_page = service.list_documents(owner, DocumentListQuery())
    foreign_page = service.list_documents(foreign, DocumentListQuery())

    assert [entry.document_id for entry in own_page.items] == [own_document.id]
    assert [entry.document_id for entry in foreign_page.items] == [foreign_document.id]
    assert service.list_documents(empty, DocumentListQuery()).items == ()
    assert own_page.next_cursor is None
    assert own_page.items[0].media_type is OriginalMediaType.PLAIN_TEXT
    assert own_page.items[0].published_at is None
    assert own_page.items[0].available_at is None
    assert own_page.items[0].captured_at == CREATED_AT
    assert "storage_key" not in own_page.model_dump_json()
    assert "checksum_sha256" not in own_page.model_dump_json()
    assert str(foreign_document.id) not in own_page.model_dump_json()


def test_document_list_keyset_handles_equal_times_and_new_imports_without_duplicates(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    with transactional_session(session_factory) as session:
        context = add_workspace(session)
        sources = [
            add_document(session, context, created_at=CREATED_AT - timedelta(days=1)),
            add_document(session, context),
            add_document(session, context),
            add_document(session, context, created_at=CREATED_AT + timedelta(days=1)),
        ]

    expected = sorted(sources, key=lambda source: (source.created_at, source.id), reverse=True)
    service = make_service(integration_database_engine)
    first = service.list_documents(context, DocumentListQuery(page_size=2))
    assert first.next_cursor is not None

    with transactional_session(session_factory) as session:
        newly_imported = add_document(session, context, created_at=CREATED_AT + timedelta(days=2))

    second = service.list_documents(
        context, DocumentListQuery(page_size=2, after=first.next_cursor)
    )

    assert [entry.document_id for entry in (*first.items, *second.items)] == [
        source.id for source in expected
    ]
    assert second.next_cursor is None
    assert newly_imported.id not in {entry.document_id for entry in second.items}
    refreshed = service.list_documents(context, DocumentListQuery(page_size=1))
    assert refreshed.items[0].document_id == newly_imported.id


def test_document_list_returns_only_the_current_version_with_an_original(
    integration_database_engine: Engine,
) -> None:
    session_factory = create_session_factory(integration_database_engine)
    with transactional_session(session_factory) as session:
        context = add_workspace(session)
        document = add_document(session, context, title="Superseded synthetic title")
        add_document(session, context, title="Incomplete capture", with_original=False)
        session.flush()
        version = DocumentVersion(
            id=uuid4(),
            workspace_id=context.workspace_id,
            document_id=document.id,
            version_number=2,
            title="Current synthetic title",
            language="en",
            content_sha256="b" * 64,
            published_at=CREATED_AT - timedelta(days=2),
            captured_at=CREATED_AT + timedelta(days=1),
        )
        original = OriginalArtifact(
            id=uuid4(),
            workspace_id=context.workspace_id,
            document_version_id=version.id,
            storage_key=f"workspaces/{context.workspace_id}/originals/{version.id}",
            media_type=OriginalMediaType.PLAIN_TEXT,
            byte_size=256,
            checksum_sha256="b" * 64,
            captured_at=version.captured_at,
        )
        session.add_all(
            [DocumentVersionRow.from_domain(version), OriginalArtifactRow.from_domain(original)]
        )
        session.flush()
        session.execute(
            update(DocumentRow)
            .where(DocumentRow.workspace_id == context.workspace_id, DocumentRow.id == document.id)
            .values(current_version_id=version.id)
        )

    page = make_service(integration_database_engine).list_documents(context, DocumentListQuery())

    assert len(page.items) == 1
    assert page.items[0].document_id == document.id
    assert page.items[0].document_version_id == version.id
    assert page.items[0].title == "Current synthetic title"
    assert page.items[0].media_type is OriginalMediaType.PLAIN_TEXT
    assert page.items[0].published_at == version.published_at
    assert page.items[0].created_at == document.created_at
    assert page.items[0].captured_at == version.captured_at
