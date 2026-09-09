"""SQLAlchemy units of work for cross-module application transactions."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass

from investment_research.infrastructure.database.session import (
    SessionFactory,
    transactional_session,
)
from investment_research.modules.documents.persistence import SqlAlchemyDocumentRepository
from investment_research.modules.documents.ports import (
    CaptureUnitOfWork,
    CaptureUnitOfWorkFactory,
    DocumentRepository,
)
from investment_research.modules.processing.persistence import SqlAlchemyProcessingRepository
from investment_research.modules.processing.ports import ProcessingRepository


@dataclass(frozen=True, slots=True)
class SqlAlchemyCaptureRepositories:
    """Repository implementations bound to the same SQLAlchemy session."""

    documents: DocumentRepository
    processing: ProcessingRepository


@contextmanager
def sqlalchemy_capture_unit_of_work(
    session_factory: SessionFactory,
) -> Iterator[CaptureUnitOfWork]:
    """Commit all canonical capture records together or roll them all back."""

    with transactional_session(session_factory) as session:
        yield SqlAlchemyCaptureRepositories(
            documents=SqlAlchemyDocumentRepository(session),
            processing=SqlAlchemyProcessingRepository(session),
        )


def create_capture_unit_of_work_factory(
    session_factory: SessionFactory,
) -> CaptureUnitOfWorkFactory:
    """Bind capture transaction creation to one process-wide session factory."""

    def create_unit_of_work() -> AbstractContextManager[CaptureUnitOfWork]:
        return sqlalchemy_capture_unit_of_work(session_factory)

    return create_unit_of_work
