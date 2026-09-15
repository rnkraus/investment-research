"""Unit tests for the bounded single-job processing runner."""

import resource
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import create_autospec
from uuid import UUID

import pytest

from investment_research import runner
from investment_research.bootstrap import WorkerContainer
from investment_research.infrastructure.parsing.pypdf import PypdfDocumentParser
from investment_research.modules.identity.application import IdentityContextProvider
from investment_research.modules.identity.domain import WorkspaceContext
from investment_research.modules.processing.application import ProcessDocumentService
from investment_research.modules.processing.domain import ProcessingRun, ProcessingState
from investment_research.modules.processing.ports import (
    ProcessingExecutionLimitError,
    ProcessingExecutor,
)
from investment_research.settings import WorkerSettings

USER_ID = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000002")
DOCUMENT_VERSION_ID = UUID("10000000-0000-4000-8000-000000000001")
PROCESSING_RUN_ID = UUID("20000000-0000-4000-8000-000000000001")
CREATED_AT = datetime(2026, 2, 20, 12, tzinfo=UTC)
LOCAL_DATABASE_URL = "postgresql+psycopg://test-user:test-password@127.0.0.1:5432/test-database"


def make_ready_run() -> ProcessingRun:
    """Build a completed run returned by the mocked processing service."""

    return ProcessingRun(
        id=PROCESSING_RUN_ID,
        workspace_id=WORKSPACE_ID,
        document_version_id=DOCUMENT_VERSION_ID,
        state=ProcessingState.LEXICAL_READY,
        parser_version="synthetic-parser-1",
        parser_config_version="parser-m0.0-v1",
        chunker_version="structure-block-1",
        chunking_config_version="chunking-m0.0-v1",
        lexical_config_version="english-lexical-v1",
        created_at=CREATED_AT,
        attempt_count=1,
        started_at=CREATED_AT + timedelta(seconds=1),
        completed_at=CREATED_AT + timedelta(seconds=2),
    )


def make_settings() -> WorkerSettings:
    """Build valid local settings without opening a database connection."""

    return WorkerSettings.model_validate({"database_url": LOCAL_DATABASE_URL})


def test_current_virtual_memory_measurement_is_available() -> None:
    assert runner.current_virtual_memory_bytes() > 0


@pytest.mark.parametrize(
    ("existing_soft", "existing_hard", "budget", "expected_soft"),
    [
        pytest.param(
            resource.RLIM_INFINITY,
            resource.RLIM_INFINITY,
            512,
            1_536,
            id="new-bound",
        ),
        pytest.param(256, resource.RLIM_INFINITY, 512, 256, id="keep-lower-soft-bound"),
        pytest.param(512, 384, 512, 384, id="respect-lower-hard-bound"),
    ],
)
def test_memory_limit_never_relaxes_existing_process_bounds(
    monkeypatch: pytest.MonkeyPatch,
    existing_soft: int,
    existing_hard: int,
    budget: int,
    expected_soft: int,
) -> None:
    applied_limits: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(
        resource,
        "getrlimit",
        lambda resource_name: (existing_soft, existing_hard),
    )
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda resource_name, limits: applied_limits.append((resource_name, limits)),
    )
    monkeypatch.setattr(runner, "current_virtual_memory_bytes", lambda: 1_024)

    runner.limit_process_memory(budget)

    assert applied_limits == [(resource.RLIMIT_AS, (expected_soft, existing_hard))]


def test_processing_timeout_arms_cancels_and_restores_signal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signal_calls: list[tuple[int, object]] = []
    timer_calls: list[tuple[int, float, float]] = []

    def record_signal(signal_number: int, handler: object) -> signal.Handlers:
        signal_calls.append((signal_number, handler))
        return signal.SIG_DFL

    def record_timer(
        timer_kind: int,
        seconds: float,
        interval: float = 0,
    ) -> tuple[float, float]:
        timer_calls.append((timer_kind, seconds, interval))
        return (0.0, 0.0)

    monkeypatch.setattr(signal, "signal", record_signal)
    monkeypatch.setattr(signal, "setitimer", record_timer)

    with runner.processing_timeout(12):
        assert timer_calls == [(signal.ITIMER_REAL, 12, 0)]

    assert signal_calls == [
        (signal.SIGALRM, runner._raise_processing_timeout),
        (signal.SIGALRM, signal.SIG_DFL),
    ]
    assert timer_calls[-1] == (signal.ITIMER_REAL, 0, 0)


def test_timeout_signal_becomes_safe_execution_limit_error() -> None:
    with pytest.raises(ProcessingExecutionLimitError, match="time limit"):
        runner._raise_processing_timeout(signal.SIGALRM, None)


def test_executor_applies_bounds_and_resolves_trusted_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = WorkspaceContext(user_id=USER_ID, workspace_id=WORKSPACE_ID)
    service = create_autospec(ProcessDocumentService, instance=True)
    service.process.return_value = make_ready_run()
    identity_provider = create_autospec(IdentityContextProvider, instance=True)
    identity_provider.resolve.return_value = context
    applied_memory_limits: list[int] = []
    applied_time_limits: list[int] = []

    @contextmanager
    def record_timeout(seconds: int) -> Iterator[None]:
        applied_time_limits.append(seconds)
        yield

    monkeypatch.setattr(runner, "limit_process_memory", applied_memory_limits.append)
    monkeypatch.setattr(runner, "processing_timeout", record_timeout)
    executor: ProcessingExecutor = runner.SingleJobProcessingExecutor(
        processing_service=service,
        identity_context_provider=identity_provider,
        timeout_seconds=30,
        memory_limit_bytes=512 * 1024 * 1024,
    )

    result = executor.execute(PROCESSING_RUN_ID)

    assert result == make_ready_run()
    assert applied_memory_limits == [512 * 1024 * 1024]
    assert applied_time_limits == [30]
    identity_provider.resolve.assert_called_once_with()
    service.process.assert_called_once_with(context, PROCESSING_RUN_ID)


def test_execute_single_job_always_closes_its_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = create_autospec(WorkerContainer, instance=True)
    executor = create_autospec(runner.SingleJobProcessingExecutor, instance=True)
    executor.execute.side_effect = RuntimeError("synthetic execution failure")
    injected_parsers: list[object] = []

    def build_legacy_container(
        settings: WorkerSettings,
        *,
        parser: object,
    ) -> WorkerContainer:
        del settings
        injected_parsers.append(parser)
        return cast(WorkerContainer, container)

    monkeypatch.setattr(runner, "build_worker_container", build_legacy_container)
    monkeypatch.setattr(
        runner,
        "SingleJobProcessingExecutor",
        lambda **dependencies: cast(runner.SingleJobProcessingExecutor, executor),
    )

    with pytest.raises(RuntimeError, match="execution failure"):
        runner.execute_single_job(make_settings(), PROCESSING_RUN_ID)

    assert len(injected_parsers) == 1
    assert isinstance(injected_parsers[0], PypdfDocumentParser)
    container.close.assert_called_once_with()


def test_main_reports_success_without_internal_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings()
    loaded_env_files: list[Path | None] = []

    def load_settings(*, env_file: Path | None) -> WorkerSettings:
        loaded_env_files.append(env_file)
        return settings

    monkeypatch.setattr(runner, "load_worker_settings", load_settings)
    monkeypatch.setattr(
        runner,
        "execute_single_job",
        lambda loaded_settings, run_id: make_ready_run(),
    )

    exit_code = runner.main([str(PROCESSING_RUN_ID)], env_file=None)

    output = capsys.readouterr()
    assert exit_code == runner.EXIT_SUCCESS
    assert str(PROCESSING_RUN_ID) in output.out
    assert ProcessingState.LEXICAL_READY.value in output.out
    assert output.err == ""
    assert loaded_env_files == [None]


def test_main_reports_persisted_processing_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings()
    failed_run = ProcessingRun.model_validate(
        {
            **make_ready_run().model_dump(),
            "state": ProcessingState.FAILED,
            "error_summary": "Safe parser failure",
        }
    )
    monkeypatch.setattr(runner, "load_worker_settings", lambda *, env_file: settings)
    monkeypatch.setattr(
        runner,
        "execute_single_job",
        lambda loaded_settings, run_id: failed_run,
    )

    exit_code = runner.main([str(PROCESSING_RUN_ID)])

    output = capsys.readouterr()
    assert exit_code == runner.EXIT_PROCESSING_FAILED
    assert output.out == ""
    assert output.err == "Processing run failed safely.\n"
    assert failed_run.error_summary is not None
    assert failed_run.error_summary not in output.err


def test_main_reports_configuration_failure_without_attempting_processing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    processing_called = False

    def record_processing(settings: WorkerSettings, run_id: UUID) -> ProcessingRun:
        nonlocal processing_called
        processing_called = True
        return make_ready_run()

    monkeypatch.setattr(
        runner,
        "load_worker_settings",
        lambda *, env_file: WorkerSettings(),
    )
    monkeypatch.setattr(runner, "execute_single_job", record_processing)

    exit_code = runner.main([str(PROCESSING_RUN_ID)])

    output = capsys.readouterr()
    assert exit_code == runner.EXIT_CONFIGURATION_FAILED
    assert processing_called is False
    assert output.out == ""
    assert output.err == "Runner configuration is invalid or incomplete.\n"


def test_main_redacts_unexpected_processing_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings()
    monkeypatch.setattr(runner, "load_worker_settings", lambda *, env_file: settings)

    def fail_safely(loaded_settings: WorkerSettings, run_id: UUID) -> ProcessingRun:
        del loaded_settings, run_id
        raise RuntimeError("sensitive database or parser detail")

    monkeypatch.setattr(runner, "execute_single_job", fail_safely)

    exit_code = runner.main([str(PROCESSING_RUN_ID)])

    output = capsys.readouterr()
    assert exit_code == runner.EXIT_PROCESSING_FAILED
    assert output.out == ""
    assert output.err == "Processing run failed safely.\n"
    assert "sensitive" not in output.err
