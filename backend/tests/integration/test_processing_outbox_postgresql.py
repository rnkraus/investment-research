"""Prove capture/outbox atomicity and purge compatibility in disposable PostgreSQL."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from investment_research.infrastructure.database import unit_of_work
from investment_research.infrastructure.database.document_deletion import (
    PostgresDocumentDeletionRepository,
)
from investment_research.infrastructure.database.outbox import SqlAlchemyOutboxRepository
from investment_research.infrastructure.database.session import (
    SessionFactory,
    create_session_factory,
    transactional_session,
)
from investment_research.modules.delivery.domain import OutboxEvent, OutboxState
from investment_research.modules.delivery.persistence import OutboxEventRow
from investment_research.modules.delivery.ports import OutboxScopeError
from investment_research.modules.documents.deletion_contracts import DeleteDocumentsCommand
from investment_research.modules.documents.persistence import (
    DocumentRow,
    DocumentVersionRow,
    OriginalArtifactRow,
)
from investment_research.modules.documents.ports import CaptureCommitUncertainError
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.persistence import ProcessingRunRow
from tests.integration.test_document_capture_postgresql import (
    CAPTURED_AT,
    PDF_CONTENT,
    create_workspace,
    make_capture_input,
    make_capture_service,
)


def test_capture_outbox_is_durable_scoped_and_concurrently_idempotent(
    integration_database_engine: Engine, tmp_path: Path
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    other = create_workspace(engine)
    result = make_capture_service(engine, tmp_path / "objects").capture_pdf(
        context, make_capture_input(), BytesIO(PDF_CONTENT)
    )
    event = OutboxEvent.for_processing(
        workspace_id=context.workspace_id,
        processing_run_id=result.processing_run_id,
        created_at=CAPTURED_AT,
    )
    sessions = create_session_factory(engine)
    barrier = Barrier(2)

    def repeat_intent() -> OutboxEvent:
        barrier.wait(timeout=10)
        with transactional_session(sessions) as session:
            return SqlAlchemyOutboxRepository(session).add_processing_event(context, event)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(repeat_intent) for _ in range(2)]
        assert [future.result(timeout=15) for future in futures] == [event, event]
    with sessions() as session:
        repository = SqlAlchemyOutboxRepository(session)
        assert repository.get_by_id(context, event.event_id) == event
        assert repository.get_by_id(other, event.event_id) is None
        assert repository.get_by_id(context, uuid4()) is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEventRow)
                .where(OutboxEventRow.workspace_id == context.workspace_id)
            )
            == 1
        )
    for target in (result.processing_run_id, uuid4()):
        foreign_event = OutboxEvent.for_processing(
            workspace_id=other.workspace_id, processing_run_id=target, created_at=CAPTURED_AT
        )
        with (
            pytest.raises(OutboxScopeError, match="intent is unavailable"),
            transactional_session(sessions) as session,
        ):
            SqlAlchemyOutboxRepository(session).add_processing_event(other, foreign_event)
    foreign_event = OutboxEvent.for_processing(
        workspace_id=other.workspace_id,
        processing_run_id=result.processing_run_id,
        created_at=CAPTURED_AT,
    )
    with pytest.raises(IntegrityError), transactional_session(sessions) as session:
        session.add(
            OutboxEventRow(**foreign_event.model_dump(), processing_run_id=result.processing_run_id)
        )


def test_outbox_write_failure_rolls_back_the_complete_capture(
    integration_database_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    service = make_capture_service(engine, tmp_path / "objects")
    original_add = SqlAlchemyOutboxRepository.add_processing_event

    def fail_after_write(
        repository: SqlAlchemyOutboxRepository, scope: WorkspaceContext, event: OutboxEvent
    ) -> OutboxEvent:
        original_add(repository, scope, event)
        raise RuntimeError("synthetic outbox transaction failure")

    monkeypatch.setattr(SqlAlchemyOutboxRepository, "add_processing_event", fail_after_write)
    with pytest.raises(RuntimeError, match="outbox transaction failure"):
        service.capture_pdf(context, make_capture_input(), BytesIO(PDF_CONTENT))
    with create_session_factory(engine)() as session:
        for row in (
            DocumentRow,
            DocumentVersionRow,
            OriginalArtifactRow,
            ProcessingRunRow,
            OutboxEventRow,
        ):
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(row)
                    .where(row.workspace_id == context.workspace_id)
                )
                == 0
            )
    assert not list((tmp_path / "objects").rglob("*.pdf"))


def test_lost_commit_acknowledgement_preserves_canonical_original_and_outbox(
    integration_database_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    original_transaction = transactional_session

    @contextmanager
    def commit_then_disconnect(sessions: SessionFactory) -> Iterator[Session]:
        with original_transaction(sessions) as session:
            yield session
        raise OSError("synthetic lost commit acknowledgement")

    monkeypatch.setattr(unit_of_work, "transactional_session", commit_then_disconnect)
    with pytest.raises(CaptureCommitUncertainError):
        make_capture_service(engine, tmp_path / "objects").capture_pdf(
            context, make_capture_input(), BytesIO(PDF_CONTENT)
        )
    with create_session_factory(engine)() as session:
        for row in (
            DocumentRow,
            DocumentVersionRow,
            OriginalArtifactRow,
            ProcessingRunRow,
            OutboxEventRow,
        ):
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(row)
                    .where(row.workspace_id == context.workspace_id)
                )
                == 1
            )
    assert len(list((tmp_path / "objects").rglob("*.pdf"))) == 1


@pytest.mark.parametrize("published", [False, True])
def test_canonical_purge_detaches_outbox_and_preserves_publication_history(
    integration_database_engine: Engine, tmp_path: Path, published: bool
) -> None:
    # Exercise canonical graph completion only, not external-object/vector absence verification.
    engine = integration_database_engine
    context = create_workspace(engine)
    result = make_capture_service(engine, tmp_path / "objects").capture_pdf(
        context, make_capture_input(), BytesIO(PDF_CONTENT)
    )
    event = OutboxEvent.for_processing(
        workspace_id=context.workspace_id,
        processing_run_id=result.processing_run_id,
        created_at=CAPTURED_AT,
    )
    sessions = create_session_factory(engine)
    published_at = CAPTURED_AT + timedelta(seconds=1) if published else None
    if published:
        with transactional_session(sessions) as session:
            session.execute(
                update(OutboxEventRow)
                .where(OutboxEventRow.event_id == event.event_id)
                .values(
                    state=OutboxState.PUBLISHED,
                    attempt_count=1,
                    next_attempt_at=None,
                    published_at=published_at,
                )
            )
    purge_at = CAPTURED_AT + timedelta(days=1)
    with transactional_session(sessions) as session:
        repository = PostgresDocumentDeletionRepository(session)
        repository.request(
            context, DeleteDocumentsCommand(document_ids=(result.document_id,)), purge_at
        )
    with pytest.raises(OutboxScopeError), transactional_session(sessions) as session:
        SqlAlchemyOutboxRepository(session).add_processing_event(context, event)
    with transactional_session(sessions) as session:
        repository = PostgresDocumentDeletionRepository(session)
        repository.complete(context, result.document_id, purge_at)
    with sessions() as session:
        row = session.scalar(
            select(OutboxEventRow).where(OutboxEventRow.event_id == event.event_id)
        )
        assert row is not None
        assert row.to_domain().state is OutboxState.CANCELLED
        assert row.processing_run_id is None
        assert row.published_at == published_at
        assert row.target_id == result.processing_run_id
        assert session.get(ProcessingRunRow, result.processing_run_id) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"attempt_count": 4},
        {"generation": 3},
        {"lease_id": uuid4()},
        {"state": OutboxState.PUBLISHED},
        {"processing_run_id": None},
    ],
)
def test_database_enforces_outbox_invariants(
    integration_database_engine: Engine, tmp_path: Path, changes: dict[str, object]
) -> None:
    engine = integration_database_engine
    context = create_workspace(engine)
    result = make_capture_service(engine, tmp_path / "objects").capture_pdf(
        context, make_capture_input(), BytesIO(PDF_CONTENT)
    )
    with (
        pytest.raises(IntegrityError),
        transactional_session(create_session_factory(engine)) as session,
    ):
        session.execute(
            update(OutboxEventRow)
            .where(OutboxEventRow.target_id == result.processing_run_id)
            .values(**changes)
        )
