"""SQLAlchemy units of work for cross-module application transactions."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass

from sqlalchemy.orm import Session

from investment_research.infrastructure.database.session import (
    SessionFactory,
    transactional_session,
)
from investment_research.infrastructure.evidence.postgresql import (
    PostgresCanonicalEvidenceSourceReader,
    PostgresCanonicalEvidenceSpanReader,
)
from investment_research.infrastructure.search.postgresql import (
    PostgresDenseCandidateQualifier,
    PostgresLexicalSearch,
)
from investment_research.modules.documents.persistence import SqlAlchemyDocumentRepository
from investment_research.modules.documents.ports import (
    CaptureUnitOfWork,
    CaptureUnitOfWorkFactory,
    DocumentReadUnitOfWork,
    DocumentReadUnitOfWorkFactory,
    DocumentRepository,
)
from investment_research.modules.evidence.persistence import SqlAlchemyEvidenceRepository
from investment_research.modules.evidence.ports import (
    CanonicalEvidenceSourceReader,
    CanonicalEvidenceSpanReader,
    EvidenceReadUnitOfWork,
    EvidenceReadUnitOfWorkFactory,
    EvidenceRepository,
)
from investment_research.modules.processing.persistence import SqlAlchemyProcessingRepository
from investment_research.modules.processing.ports import (
    ProcessingReadUnitOfWork,
    ProcessingReadUnitOfWorkFactory,
    ProcessingRepository,
    ProcessingUnitOfWork,
    ProcessingUnitOfWorkFactory,
)
from investment_research.modules.research.persistence import SqlAlchemyResearchRepository
from investment_research.modules.research.ports import (
    AnswerValidationUnitOfWork,
    AnswerValidationUnitOfWorkFactory,
    ResearchRepository,
    ResearchRetrievalUnitOfWork,
    ResearchRetrievalUnitOfWorkFactory,
)
from investment_research.modules.search.persistence import SqlAlchemyRetrievalRepository
from investment_research.modules.search.ports import (
    DenseCandidateQualifier,
    LexicalSearch,
    RetrievalRepository,
)


@dataclass(frozen=True, slots=True)
class SqlAlchemyCaptureRepositories:
    """Repository implementations bound to the same SQLAlchemy session."""

    documents: DocumentRepository
    processing: ProcessingRepository


@dataclass(frozen=True, slots=True)
class SqlAlchemyDocumentReadRepositories:
    """Document repository bound to one short-lived read session."""

    documents: DocumentRepository


@dataclass(frozen=True, slots=True)
class SqlAlchemyEvidenceReadRepositories:
    """Canonical citation reader bound to one short-lived read session."""

    evidence_spans: CanonicalEvidenceSpanReader


@dataclass(frozen=True, slots=True)
class SqlAlchemyProcessingRepositories:
    """Processing repository bound to one short-lived transaction."""

    processing: ProcessingRepository


@dataclass(frozen=True, slots=True)
class SqlAlchemyProcessingReadRepositories:
    """Processing repository bound to one short-lived read session."""

    processing: ProcessingRepository


@dataclass(frozen=True, slots=True)
class SqlAlchemyResearchRetrievalRepositories:
    """Research and retrieval collaborators bound to one SQLAlchemy session."""

    research: ResearchRepository
    lexical_search: LexicalSearch
    dense_qualifier: DenseCandidateQualifier
    retrieval: RetrievalRepository


@dataclass(frozen=True, slots=True)
class SqlAlchemyAnswerValidationRepositories:
    """Answer and Evidence collaborators bound to one SQLAlchemy session."""

    research: ResearchRepository
    evidence: EvidenceRepository
    evidence_sources: CanonicalEvidenceSourceReader
    _session: Session

    def flush(self) -> None:
        """Flush staged dependency roots while preserving the open transaction."""

        self._session.flush()


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


@contextmanager
def sqlalchemy_document_read_unit_of_work(
    session_factory: SessionFactory,
) -> Iterator[DocumentReadUnitOfWork]:
    """Expose eligible document reads without committing a write transaction."""

    with session_factory() as session:
        yield SqlAlchemyDocumentReadRepositories(
            documents=SqlAlchemyDocumentRepository(session),
        )


@contextmanager
def sqlalchemy_evidence_read_unit_of_work(
    session_factory: SessionFactory,
) -> Iterator[EvidenceReadUnitOfWork]:
    """Expose canonical citation reads without committing a transaction."""

    with session_factory() as session:
        yield SqlAlchemyEvidenceReadRepositories(
            evidence_spans=PostgresCanonicalEvidenceSpanReader(session),
        )


@contextmanager
def sqlalchemy_processing_unit_of_work(
    session_factory: SessionFactory,
) -> Iterator[ProcessingUnitOfWork]:
    """Commit one processing state change and its related output atomically."""

    with transactional_session(session_factory) as session:
        yield SqlAlchemyProcessingRepositories(
            processing=SqlAlchemyProcessingRepository(session),
        )


@contextmanager
def sqlalchemy_processing_read_unit_of_work(
    session_factory: SessionFactory,
) -> Iterator[ProcessingReadUnitOfWork]:
    """Expose one scoped status read without opening a write transaction."""

    with session_factory() as session:
        yield SqlAlchemyProcessingReadRepositories(
            processing=SqlAlchemyProcessingRepository(session),
        )


@contextmanager
def sqlalchemy_research_retrieval_unit_of_work(
    session_factory: SessionFactory,
) -> Iterator[ResearchRetrievalUnitOfWork]:
    """Commit one request and its canonically qualified retrieval result atomically."""

    with transactional_session(session_factory) as session:
        yield SqlAlchemyResearchRetrievalRepositories(
            research=SqlAlchemyResearchRepository(session),
            lexical_search=PostgresLexicalSearch(session),
            dense_qualifier=PostgresDenseCandidateQualifier(session),
            retrieval=SqlAlchemyRetrievalRepository(session),
        )


@contextmanager
def sqlalchemy_answer_validation_unit_of_work(
    session_factory: SessionFactory,
) -> Iterator[AnswerValidationUnitOfWork]:
    """Validate canonical evidence and commit its complete answer aggregate atomically."""

    with transactional_session(session_factory) as session:
        yield SqlAlchemyAnswerValidationRepositories(
            research=SqlAlchemyResearchRepository(session),
            evidence=SqlAlchemyEvidenceRepository(session),
            evidence_sources=PostgresCanonicalEvidenceSourceReader(session),
            _session=session,
        )


def create_capture_unit_of_work_factory(
    session_factory: SessionFactory,
) -> CaptureUnitOfWorkFactory:
    """Bind capture transaction creation to one process-wide session factory."""

    def create_unit_of_work() -> AbstractContextManager[CaptureUnitOfWork]:
        return sqlalchemy_capture_unit_of_work(session_factory)

    return create_unit_of_work


def create_document_read_unit_of_work_factory(
    session_factory: SessionFactory,
) -> DocumentReadUnitOfWorkFactory:
    """Bind document-read session creation to one process-wide session factory."""

    def create_unit_of_work() -> AbstractContextManager[DocumentReadUnitOfWork]:
        return sqlalchemy_document_read_unit_of_work(session_factory)

    return create_unit_of_work


def create_evidence_read_unit_of_work_factory(
    session_factory: SessionFactory,
) -> EvidenceReadUnitOfWorkFactory:
    """Bind citation-resolution reads to one process-wide session factory."""

    def create_unit_of_work() -> AbstractContextManager[EvidenceReadUnitOfWork]:
        return sqlalchemy_evidence_read_unit_of_work(session_factory)

    return create_unit_of_work


def create_processing_unit_of_work_factory(
    session_factory: SessionFactory,
) -> ProcessingUnitOfWorkFactory:
    """Bind processing transactions to one process-wide session factory."""

    def create_unit_of_work() -> AbstractContextManager[ProcessingUnitOfWork]:
        return sqlalchemy_processing_unit_of_work(session_factory)

    return create_unit_of_work


def create_processing_read_unit_of_work_factory(
    session_factory: SessionFactory,
) -> ProcessingReadUnitOfWorkFactory:
    """Bind processing-status reads to one process-wide session factory."""

    def create_unit_of_work() -> AbstractContextManager[ProcessingReadUnitOfWork]:
        return sqlalchemy_processing_read_unit_of_work(session_factory)

    return create_unit_of_work


def create_research_retrieval_unit_of_work_factory(
    session_factory: SessionFactory,
) -> ResearchRetrievalUnitOfWorkFactory:
    """Bind research-retrieval transaction creation to one process-wide session factory."""

    def create_unit_of_work() -> AbstractContextManager[ResearchRetrievalUnitOfWork]:
        return sqlalchemy_research_retrieval_unit_of_work(session_factory)

    return create_unit_of_work


def create_answer_validation_unit_of_work_factory(
    session_factory: SessionFactory,
) -> AnswerValidationUnitOfWorkFactory:
    """Bind answer-validation transaction creation to one process-wide session factory."""

    def create_unit_of_work() -> AbstractContextManager[AnswerValidationUnitOfWork]:
        return sqlalchemy_answer_validation_unit_of_work(session_factory)

    return create_unit_of_work
