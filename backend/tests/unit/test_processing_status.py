"""Unit tests for workspace-scoped processing-status reads."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast
from unittest.mock import create_autospec
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import Select
from sqlalchemy.orm import Session

from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.application import (
    GetProcessingRunService,
    ProcessingRunUnavailableError,
)
from investment_research.modules.processing.contracts import ProcessingStatusQuery
from investment_research.modules.processing.domain import ProcessingRun, ProcessingState
from investment_research.modules.processing.persistence import (
    ProcessingRunRow,
    SqlAlchemyProcessingRepository,
)
from investment_research.modules.processing.ports import (
    ProcessingReadUnitOfWork,
    ProcessingReadUnitOfWorkFactory,
    ProcessingStatusRepository,
)

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
DOCUMENT_VERSION_ID = UUID("10000000-0000-4000-8000-000000000001")
PROCESSING_RUN_ID = UUID("20000000-0000-4000-8000-000000000001")
CREATED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)


class RecordingProcessingReadUnitOfWork:
    """Expose one mocked processing repository through the read contract."""

    def __init__(self) -> None:
        self.repository_mock = create_autospec(ProcessingStatusRepository, instance=True)
        self.processing = cast(ProcessingStatusRepository, self.repository_mock)
        self.enter_count = 0
        self.exit_count = 0

    @contextmanager
    def open(self) -> Iterator[ProcessingReadUnitOfWork]:
        """Record the complete lifetime of one status read."""

        self.enter_count += 1
        try:
            yield self
        finally:
            self.exit_count += 1


def make_context() -> WorkspaceContext:
    """Return the trusted local context used by status tests."""

    return WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)


def make_run() -> ProcessingRun:
    """Build one visible captured processing run."""

    return ProcessingRun(
        id=PROCESSING_RUN_ID,
        workspace_id=WORKSPACE_ID,
        document_version_id=DOCUMENT_VERSION_ID,
        state=ProcessingState.CAPTURED,
        parser_version="synthetic-parser-1",
        parser_config_version="parser-m0.0-v1",
        chunker_version="structure-block-1",
        chunking_config_version="chunking-m0.0-v1",
        lexical_config_version="english-lexical-v1",
        embedding_config_version="m0.0-embedding-disabled-v1",
        dense_index_config_version="m0.0-dense-index-disabled-v1",
        created_at=CREATED_AT,
    )


def make_service(
    unit_of_work: RecordingProcessingReadUnitOfWork,
) -> GetProcessingRunService:
    """Bind the service to one observable read-unit factory."""

    factory: ProcessingReadUnitOfWorkFactory = unit_of_work.open
    return GetProcessingRunService(unit_of_work_factory=factory)


def test_get_processing_run_returns_scoped_snapshot_and_closes_read_session() -> None:
    unit_of_work = RecordingProcessingReadUnitOfWork()
    run = make_run()
    unit_of_work.repository_mock.get_run_by_id.return_value = run

    result = make_service(unit_of_work).get(make_context(), PROCESSING_RUN_ID)

    assert result == run
    assert unit_of_work.enter_count == unit_of_work.exit_count == 1
    unit_of_work.repository_mock.get_run_by_id.assert_called_once_with(
        make_context(),
        PROCESSING_RUN_ID,
    )


def test_get_processing_run_hides_every_unavailable_identifier() -> None:
    unit_of_work = RecordingProcessingReadUnitOfWork()
    unit_of_work.repository_mock.get_run_by_id.return_value = None

    with pytest.raises(ProcessingRunUnavailableError, match="unavailable"):
        make_service(unit_of_work).get(make_context(), PROCESSING_RUN_ID)

    assert unit_of_work.enter_count == unit_of_work.exit_count == 1


def test_processing_status_batch_is_bounded_and_rejects_client_workspace() -> None:
    assert (
        len(
            ProcessingStatusQuery(
                document_version_ids=(DOCUMENT_VERSION_ID,) * 100
            ).document_version_ids
        )
        == 100
    )
    with pytest.raises(ValidationError):
        ProcessingStatusQuery(document_version_ids=(DOCUMENT_VERSION_ID,) * 101)
    with pytest.raises(ValidationError):
        ProcessingStatusQuery.model_validate(
            {"document_version_ids": [], "workspace_id": str(WORKSPACE_ID)}
        )


def test_latest_processing_status_uses_one_batch_and_closes_read_session() -> None:
    unit_of_work = RecordingProcessingReadUnitOfWork()
    query = ProcessingStatusQuery(document_version_ids=(DOCUMENT_VERSION_ID,))
    run = make_run()
    unit_of_work.repository_mock.list_latest_runs_for_versions.return_value = (run,)

    result = make_service(unit_of_work).get_latest_for_versions(make_context(), query)

    assert result == (run,)
    assert unit_of_work.enter_count == unit_of_work.exit_count == 1
    unit_of_work.repository_mock.list_latest_runs_for_versions.assert_called_once_with(
        make_context(), query
    )
    unit_of_work.repository_mock.get_run_by_id.assert_not_called()


def test_latest_processing_status_closes_read_session_on_database_failure() -> None:
    unit_of_work = RecordingProcessingReadUnitOfWork()
    unit_of_work.repository_mock.list_latest_runs_for_versions.side_effect = RuntimeError(
        "synthetic read failure"
    )

    with pytest.raises(RuntimeError, match="synthetic read failure"):
        make_service(unit_of_work).get_latest_for_versions(
            make_context(), ProcessingStatusQuery(document_version_ids=(DOCUMENT_VERSION_ID,))
        )

    assert unit_of_work.enter_count == unit_of_work.exit_count == 1


def test_empty_processing_status_batch_never_opens_a_session() -> None:
    unit_of_work = RecordingProcessingReadUnitOfWork()

    assert (
        make_service(unit_of_work).get_latest_for_versions(
            make_context(), ProcessingStatusQuery(document_version_ids=())
        )
        == ()
    )
    assert unit_of_work.enter_count == unit_of_work.exit_count == 0


def test_processing_status_repository_ranks_scoped_versions_in_one_query() -> None:
    session = create_autospec(Session, instance=True)
    run = make_run()
    session.scalars.return_value = [ProcessingRunRow.from_domain(run)]
    query = ProcessingStatusQuery(document_version_ids=(DOCUMENT_VERSION_ID,))

    result = SqlAlchemyProcessingRepository(session).list_latest_runs_for_versions(
        make_context(), query
    )

    assert result == (run,)
    session.scalars.assert_called_once()
    statement = cast(Select[tuple[ProcessingRunRow]], session.scalars.call_args.args[0])
    compiled = statement.compile()
    sql = str(compiled)
    assert "row_number() OVER (PARTITION BY processing_runs.document_version_id" in sql
    assert "ORDER BY processing_runs.created_at DESC, processing_runs.id DESC" in sql
    assert "processing_runs.document_version_id IN" in sql
    assert "ranked_processing_runs.position =" in sql
    assert list(compiled.params.values()).count(WORKSPACE_ID) == 2
    session.commit.assert_not_called()


def test_empty_processing_status_repository_batch_does_not_query() -> None:
    session = create_autospec(Session, instance=True)

    assert (
        SqlAlchemyProcessingRepository(session).list_latest_runs_for_versions(
            make_context(), ProcessingStatusQuery(document_version_ids=())
        )
        == ()
    )
    session.scalars.assert_not_called()
