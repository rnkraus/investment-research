"""Verify document company edits through the production transaction adapter."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from investment_research.infrastructure.database.document_companies import (
    create_document_company_service,
)
from investment_research.infrastructure.database.document_deletion import (
    PostgresDocumentDeletionRepository,
)
from investment_research.infrastructure.database.session import create_session_factory
from investment_research.modules.companies.persistence import SqlAlchemyCompanyRepository
from investment_research.modules.documents.company_assignment import (
    CompanyAssignmentConflictError,
    CompanyAssignmentUnavailableError,
    DocumentCompanyAssignment,
    ReplaceDocumentCompaniesCommand,
)
from investment_research.modules.documents.deletion_contracts import DeleteDocumentsCommand
from investment_research.modules.documents.persistence import (
    DocumentCompanyRevisionRow,
    DocumentCompanyRow,
)
from investment_research.modules.identity.domain import WorkspaceContext
from tests.integration.test_company_postgresql import make_company
from tests.integration.test_document_deletion_postgresql import PURGE_TIME, persist_unclaimed_source


def add_company(engine: Engine, context: WorkspaceContext, name: str = "Northstar") -> UUID:
    company = make_company(context, name)
    with Session(engine) as session, session.begin():
        SqlAlchemyCompanyRepository(session).add(context, company)
    return company.id


def replacement(
    snapshot: DocumentCompanyAssignment, *company_ids: UUID
) -> ReplaceDocumentCompaniesCommand:
    return ReplaceDocumentCompaniesCommand(
        expected_document_version_id=snapshot.document_version_id,
        expected_revision=snapshot.revision,
        company_ids=company_ids,
    )


def test_edit_preserves_retained_review_and_rejects_old_snapshots_after_clear(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    source = persist_unclaimed_source(engine)
    context, document_id = source.context, source.document.id
    service = create_document_company_service(create_session_factory(engine))
    initial = service.get(context, document_id)
    assert initial.companies == () and initial.revision == 0
    first = add_company(engine, context)
    second = add_company(engine, context, "Blue Ridge")
    edited = service.replace(context, document_id, replacement(initial, first))
    assert edited.revision == 1
    assert service.get(context, document_id) == edited
    reviewer = WorkspaceContext(workspace_id=context.workspace_id, user_id=uuid4())
    expanded = service.replace(reviewer, document_id, replacement(edited, first, second))
    assert expanded.revision == 2
    assert [company.company_id for company in expanded.companies] == [second, first]
    assert service.replace(context, document_id, replacement(expanded, second, first)) == expanded

    with Session(engine) as session:
        retained = session.get(DocumentCompanyRow, (context.workspace_id, document_id, first))
        added = session.get(DocumentCompanyRow, (context.workspace_id, document_id, second))
        assert retained is not None and retained.reviewed_by == context.user_id
        assert added is not None and added.reviewed_by == reviewer.user_id

    cleared = service.replace(context, document_id, replacement(expanded))
    assert cleared.revision == 3 and cleared.companies == ()
    assert service.get(context, document_id) == cleared
    with pytest.raises(CompanyAssignmentConflictError):
        service.replace(context, document_id, replacement(initial))


def test_invalid_company_or_version_keeps_the_current_assignment(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    source = persist_unclaimed_source(engine)
    foreign = persist_unclaimed_source(engine)
    context, document_id = source.context, source.document.id
    service = create_document_company_service(create_session_factory(engine))
    company = add_company(engine, context)
    foreign_company = add_company(engine, foreign.context)
    initial = service.get(context, document_id)
    current = service.replace(context, document_id, replacement(initial, company))

    for invalid_company in (foreign_company, uuid4()):
        with pytest.raises(CompanyAssignmentUnavailableError, match=r"^company is unavailable$"):
            service.replace(context, document_id, replacement(current, company, invalid_company))
    for hidden_document in (foreign.document.id, uuid4()):
        with pytest.raises(CompanyAssignmentUnavailableError, match=r"^document is unavailable$"):
            service.get(context, hidden_document)
    with pytest.raises(CompanyAssignmentConflictError):
        service.replace(
            context,
            document_id,
            ReplaceDocumentCompaniesCommand(
                expected_document_version_id=uuid4(),
                expected_revision=current.revision,
                company_ids=(),
            ),
        )
    assert service.get(context, document_id) == current


def test_competing_initial_edits_accept_only_one_writer(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    source = persist_unclaimed_source(engine)
    context, document_id = source.context, source.document.id
    service = create_document_company_service(create_session_factory(engine))
    initial = service.get(context, document_id)
    company = add_company(engine, context)
    other_company = add_company(engine, context, "Other")
    barrier = Barrier(2)

    def edit(company_id: UUID) -> DocumentCompanyAssignment | None:
        barrier.wait(timeout=5)
        try:
            return service.replace(context, document_id, replacement(initial, company_id))
        except CompanyAssignmentConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(edit, company), pool.submit(edit, other_company)
        results = [first.result(timeout=10), second.result(timeout=10)]
    (winner,) = [result for result in results if result is not None]
    assert winner.revision == 1
    assert service.get(context, document_id) == winner


def test_pending_document_rejects_edits_and_purge_removes_revision(
    integration_database_engine: Engine,
) -> None:
    engine = integration_database_engine
    source = persist_unclaimed_source(engine)
    context, document_id = source.context, source.document.id
    service = create_document_company_service(create_session_factory(engine))
    company = add_company(engine, context)
    initial = service.get(context, document_id)
    current = service.replace(context, document_id, replacement(initial, company))
    with Session(engine) as session, session.begin():
        PostgresDocumentDeletionRepository(session).request(
            context, DeleteDocumentsCommand(document_ids=(document_id,)), PURGE_TIME
        )

    with pytest.raises(CompanyAssignmentUnavailableError):
        service.get(context, document_id)
    with pytest.raises(CompanyAssignmentUnavailableError):
        service.replace(context, document_id, replacement(current))

    with Session(engine) as session, session.begin():
        PostgresDocumentDeletionRepository(session).complete(context, document_id, PURGE_TIME)
    with Session(engine) as session:
        assert session.get(DocumentCompanyRevisionRow, (context.workspace_id, document_id)) is None
        assert session.get(DocumentCompanyRow, (context.workspace_id, document_id, company)) is None
